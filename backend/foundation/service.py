"""Structured, non-authorizing goal tools. No natural language routing."""
import logging
import sqlite3
from uuid import uuid4

from pydantic import ValidationError
from agent.schemas import TransferMoneyArguments
from lifecycle.store import TaskError

log = logging.getLogger(__name__)


from .contracts import Call, feedback, classify
from .drafts import GoalStore, public, merge, normalized, inspect

def run(call, principal, session_id):
    response=_protected_run(call,principal,session_id)
    if call.reference and response['status'] not in {'completed','saved_incomplete','ambiguous'}:
        try:
            current=GoalStore().load(principal,call.reference)
            if current['status']=='preparing':
                response['status']='commit_unknown'
                details=response['data'] if isinstance(response['data'],dict) else {}
                response['data']=dict(details,draft=public(current),preparation_outcome='unknown',task_created=None)
                response['effects']=['draft_reserved','task_preparation_attempted']
                response['next_actions']=['draft.read','task.recover']
        except Exception:
            pass  # The primary failure retains its diagnostic trace; no state claim.
    if response['status'] in {'completed','saved_incomplete','ambiguous','invalid_parameters'}:
        from .snapshot import associate
        data=response.get('data')
        refs=[]
        if isinstance(data,dict):
            if isinstance(data.get('draft'),dict): refs.append(data['draft']['reference'])
            if data.get('reference','').startswith('tg_'): refs.append(data['reference'])
            task=data.get('prepared',{}).get('task',{}) if isinstance(data.get('prepared'),dict) else data
            if isinstance(task.get('task'),dict) and task['task'].get('task_id'): refs.append(task['task']['task_id'])
        try:
            for reference in dict.fromkeys(refs): associate(principal,session_id,reference)
        except Exception:
            # The business operation already has a durable reference. Never
            # disguise it as not executed just because its context link failed.
            log.exception('context association failed trace=%s',response['trace_id'])
            response['context_association_status']='unavailable'
            response['next_actions']=list(dict.fromkeys([*response['next_actions'],'context.focus']))
    return response


def _protected_run(call, principal, session_id):
    """Only invoke with authenticated server-injected principal/session, under session lock."""
    trace=uuid4().hex
    try:
        return _run(call,principal,session_id)
    except ValidationError as exc:
        return feedback('invalid_parameters','schema_invalid',data={'field_errors':[
            dict(field='.'.join(map(str,e['loc'])),code=e['type']) for e in exc.errors()]},trace=trace)
    except TaskError as exc:
        status = classify(exc.code)
        return feedback(status,exc.code,next_actions=['task.verify','task.list'] if status=='commit_unknown' else [],trace=trace)
    except Exception as exc:
        from billvision.provider import VisionError
        from storage.errors import CommitUnknown, StorageError
        from security.strong_auth import StrongAuthError
        from security.identity import IdentityError
        if isinstance(exc,(StrongAuthError,IdentityError)):
            return feedback('denied',exc.code,trace=trace)
        if isinstance(exc,VisionError): return feedback(classify(exc.code),exc.code,trace=trace)
        if isinstance(exc,CommitUnknown) or isinstance(exc,StorageError) and str(exc.code)=='commit_outcome_unknown':
            return feedback('commit_unknown','commit_unknown',next_actions=['task.verify','task.list','draft.read'],trace=trace)
        if isinstance(exc,(sqlite3.Error,StorageError)):
            log.exception('tool service failure trace=%s',trace)
            return feedback('service_failure','service_unavailable',next_actions=['draft.read','task.verify'],trace=trace)
        log.exception('unexpected tool failure trace=%s',trace)
        return feedback('internal_error','internal_error',next_actions=['draft.read','task.verify'],trace=trace)


def _run(call,p,sid):
    from card_service import get_cards
    from transfer_service import get_transfer_recipients
    from lifecycle.store import TaskStore
    from lifecycle.service import public as task_public
    from lifecycle.application import prepare_structured,perform
    from execution.sessions import session_for
    from lifecycle.models import CreateTaskRequest,ModifyRequest,RevisionRequest
    from security.totp import independent_protocol
    op=call.operation; store=GoalStore()
    if op in {'query.context','context.snapshot','context.focus'}:
        from .snapshot import snapshot, set_focus
        if op=='context.focus': set_focus(p,sid,call.reference)
        return feedback(data=snapshot(p,sid,limit=call.limit,session_hash=call.reference if op=='context.snapshot' else None))
    if op=='candidates':
        cards=get_cards(p); recipients=get_transfer_recipients(p)
        if not cards['success'] or not recipients['success']: raise TaskError('service_unavailable')
        return feedback(data=dict(cards=cards['data'],recipients=recipients['data'],
            accounts='Existing application exposes owned cards with balances; no separate account tool'))
    if op=='task.list': return feedback(data=[task_public(t) for t in TaskStore().list(p,limit=call.limit)])
    if op=='task.recover':
        value=store.load(p,call.reference)
        if call.revision!=value['revision']: raise TaskError('stale_revision')
        return recover_preparation(p,value)
    if op.startswith('task.') and op!='task.prepare':
        record=TaskStore().load(call.reference,p)
        if op in {'task.read','task.verify'}:
            # Receipt reconciliation is performed by original lifecycle recovery, never resubmit here.
            unknown=any(o.state in {'unknown','inflight'} for o in record.operations.values())
            return feedback('commit_unknown' if unknown else 'completed','commit_unknown' if unknown else 'ok',
                data=task_public(record),next_actions=['secure task recovery with original operation key'] if unknown else [])
        if call.revision is None: raise TaskError('revision_required')
        if 'revision' in call.fields: raise TaskError('invalid_parameters')
        body=dict(revision=call.revision,**call.fields)
        token=independent_protocol.set(True)
        try: data=perform(sid,call.reference,(ModifyRequest if op=='task.modify' else RevisionRequest).model_validate(body),op.split('.')[1],p)
        finally: independent_protocol.reset(token)
        return feedback(data=data,effects=['task_transition'],next_actions=['task.read'])
    if op=='draft.create':
        if call.kind is None: raise TaskError('kind_required')
        value=store.save(p,call.kind,normalized(call.kind,call.fields))
    else:
        value=store.load(p,call.reference)
        if op in {'draft.read','result.read'}:
            if (op=='result.read') != (value['kind']=='result'): raise TaskError('invalid_reference_kind')
            return feedback(data=public(value) if op=='result.read' else dict(draft=public(value),**inspect(p,value)))
        if value['kind']=='result': raise TaskError('invalid_reference_kind')
        if op not in {'draft.validate'}:
            if call.revision!=value['revision']: raise TaskError('stale_revision')
            if value['status']!='open':
                return feedback('business_condition','draft_not_open',data=public(value),next_actions=['task.read','task.modify','task.verify'])
        if op=='draft.close':
            return feedback(data=public(store.save(p,value['kind'],value['fields'],previous=value,status='closed')),effects=['draft_closed'])
        if op=='draft.patch':
            fields=merge(value['fields'],call.fields)
            if value['kind']=='transfer':
                if 'recipient_label' in call.fields and 'recipient_id' not in call.fields: fields.pop('recipient_id',None)
                if 'recipient_id' in call.fields and 'recipient_label' not in call.fields: fields.pop('recipient_label',None)
                fields.pop('_preparation',None)
            value=store.save(p,value['kind'],normalized(value['kind'],fields),previous=value)
    if op in {'draft.read','draft.create','draft.patch','draft.validate'}:
        check=inspect(p,value)
        dependency=next((e['code'] for e in check['field_errors'] if e['code'] in {'service_unavailable','internal_error'}),None)
        return feedback('service_failure' if dependency=='service_unavailable' else 'internal_error' if dependency else 'saved_incomplete' if check['missing_fields'] else 'ambiguous' if check['candidates'] else 'invalid_parameters' if check['field_errors'] else 'completed',
            'draft_saved' if op in {'draft.create','draft.patch'} else 'draft_validated',data=dict(draft=public(value),**check),
            effects=['draft_saved'] if op in {'draft.create','draft.patch'} else [],next_actions=['draft.patch','draft.validate','draft.close'])
    check=inspect(p,value)
    if not check['ready']:
        dependency=next((e['code'] for e in check['field_errors'] if e['code'] in {'service_unavailable','internal_error'}),None)
        return feedback('service_failure' if dependency=='service_unavailable' else 'internal_error' if dependency else 'missing_information' if check['missing_fields'] else 'ambiguous' if check['candidates'] else 'invalid_parameters',dependency or 'draft_not_ready',data=dict(draft=public(value),**check))
    if op=='query.execute':
        if value['kind']!='query': raise TaskError('invalid_reference_kind')
        from storage.config import pg02_enabled
        if not pg02_enabled(): raise TaskError('query_backend_unsupported')
        from .queries import execute
        return execute(p,value)
    if op=='task.prepare':
        if value['kind']!='transfer': raise TaskError('unsupported_draft_kind')
        f=value['fields']; args=TransferMoneyArguments.model_validate(dict(from_card_id=f['from_card_id'],
            recipient_id=check['resolved_recipient']['recipient_id'],amount=f['amount'],remark=f.get('remark'))).model_dump(mode='json')
        request=CreateTaskRequest.model_validate(dict(plan=dict(contract=dict(contract_version='1.0',goal='结构化转账草稿',
            domains=['transfer'],requested_tools=['transfer_money'],constraints=[]),steps=[dict(step_id='transfer',
            purpose='预览转账',tool_name='transfer_money',arguments=args,depends_on=[])])))
        # Durable one-shot reservation before calling the existing preview service.
        # Failure leaves preparing, never suggests blindly creating another task.
        reserved=store.save(p,value['kind'],dict(f,_preparation=dict(task_id=str(uuid4()),source_revision=value['revision'])),previous=value,status='preparing')
        token=independent_protocol.set(True)
        try:
            data=prepare_structured(sid,request,p,preparation_task_id=reserved['fields']['_preparation']['task_id'])
        except Exception as error:
            if getattr(error,'preparation_no_task',False):
                saved=store.save(p,value['kind'],f,previous=reserved)
                result=feedback(classify(error.code),error.code,data=dict(draft=public(saved),preparation_outcome='not_created',task_created=False),
                    effects=['draft_saved'],next_actions=['draft.patch','draft.close','task.prepare'],retry=True)
                result['retry']['prerequisite']='resolve '+error.code+'; read current draft reference and revision'
                return result
            log.exception('prepare interrupted reference=%s',value['reference'])
            return recover_preparation(p,reserved)
        finally: independent_protocol.reset(token)
        linked=dict(reserved['fields'],task_id=data['task']['task']['task_id'])
        saved=store.save(p,value['kind'],linked,previous=reserved,status='linked')
        return feedback(data=dict(draft=public(saved),prepared=data),effects=['draft_reserved','task_prepared'],next_actions=['task.read','task.modify','secure confirmation protocol'])
    raise TaskError('unsupported_operation')


def recover_preparation(principal,value):
    """Read a reserved UUID only. Absence is not evidence of non-creation."""
    from lifecycle.store import TaskStore
    from lifecycle.service import public as task_public
    store=GoalStore()
    if value['status'] not in {'preparing','linked'}: raise TaskError('preparation_not_pending')
    attempt=value['fields'].get('_preparation',{})
    tid=attempt.get('task_id')
    if not tid:
        return feedback('commit_unknown','legacy_preparation_unverifiable',data=dict(draft=public(value),task_created=None,
            preparation_outcome='unknown',limitation='legacy draft has no durable preparation UUID; operator investigation required'),next_actions=['draft.read'])
    try:
        record=TaskStore().load(tid,principal)
    except TaskError as error:
        if error.code!='task_not_found': raise
        return feedback('commit_unknown','preparation_unresolved',data=dict(draft=public(value),task_created=None,
            preparation_outcome='unknown',verification_reference=tid),next_actions=['task.recover'])
    if value['status']=='preparing':
        value=store.save(principal,value['kind'],dict(value['fields'],task_id=tid),previous=value,status='linked')
    return feedback(data=dict(draft=public(value),task=task_public(record),task_created=True,preparation_outcome='linked'),
        effects=['existing_task_linked'],next_actions=['task.read','task.modify','task.resume'])
