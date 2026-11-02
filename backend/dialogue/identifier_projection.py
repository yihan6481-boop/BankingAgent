"""Opaque UUID metadata in reviewed server response shapes, never free text.

This is presentation, not authorization. TaskStore canonical/owner/digest reads
still precede associations and every operation. A key named task_id alone grants
no exemption. Only the specific identifier slots below survive numeric scanning.
"""
from datetime import datetime
from typing import get_args
from uuid import UUID
import re
from agent.schemas import TaskStatus,StepStatus

LIFECYCLE=('active','waiting','paused','scheduled','manual','cancelled','completed','failed')


def uuid4_text(value):
    if not isinstance(value,str):return False
    try:parsed=UUID(value)
    except ValueError:return False
    return parsed.version==4 and str(parsed)==value


def positive_revision(value):return type(value) is int and value>=1


PAYMENT_DOCUMENT_STATES={'extracting','review','ready','prepared','unknown'}
PAYMENT_PREPARATION_STATES={'preparing','draft_ready','prepared','cancelled'}


def _payment_task_paths(value):
    """Reviewed payment-1.0 DTO slots that carry the canonical lifecycle task.

    Only these exact server-constructed shapes restore a task UUID. A key named
    task_id in free text, arbitrary dicts or unknown DTOs grants no exemption.
    """
    paths=[]
    data=value.get('data')
    if isinstance(data,dict):
        task=data.get('task')
        if (isinstance(task,dict) and uuid4_text(task.get('task_id'))
                and positive_revision(task.get('revision'))
                and task.get('status') in LIFECYCLE and set(task)<={'task_id','revision','status','preview'}):
            paths.append((('data','task','task_id'),task['task_id']))
        preparation=data.get('preparation')
        if (isinstance(preparation,dict) and uuid4_text(preparation.get('task_id'))
                and isinstance(preparation.get('preparation_id'),str)
                and re.fullmatch('pp_[0-9a-f]{32}',preparation['preparation_id'])
                and preparation.get('state') in PAYMENT_PREPARATION_STATES):
            paths.append((('data','preparation','task_id'),preparation['task_id']))
        document=data.get('document')
        if (isinstance(document,dict) and isinstance(document.get('document_id'),str)
                and re.fullmatch('pd_[0-9a-f]{32}',document['document_id'])
                and document.get('state') in PAYMENT_DOCUMENT_STATES):
            single=document.get('preparation')
            if (isinstance(single,dict) and uuid4_text(single.get('task_id'))
                    and single.get('state') in PAYMENT_PREPARATION_STATES):
                paths.append((('data','document','preparation','task_id'),single['task_id']))
            for index,item in enumerate(document.get('preparations') or []):
                if (isinstance(item,dict) and uuid4_text(item.get('task_id'))
                        and item.get('state') in PAYMENT_PREPARATION_STATES):
                    paths.append((('data','document','preparations',index,'task_id'),item['task_id']))
    # payment_preparation_read returns the reviewed preparation DTO directly.
    if (isinstance(value.get('preparation_id'),str)
            and re.fullmatch('pp_[0-9a-f]{32}',value['preparation_id'])
            and uuid4_text(value.get('task_id'))
            and value.get('state') in PAYMENT_PREPARATION_STATES):
        paths.append((('task_id',),value['task_id']))
    return paths


def metadata_paths(value):
    """Recognize stable full TaskView, linked goal, and reference-only DTOs.
    All other fields, including arguments/notes/purpose, retain normal scanning.
    """
    from security.redaction import redact
    focus_scope='understanding hint only; not authorization or forced pronoun binding'
    paths=[];task=value.get('task')
    if (isinstance(task,dict) and positive_revision(value.get('revision')) and value.get('mode') in ('single','workflow')
            and value.get('status') in LIFECYCLE and value.get('kind','task')=='task'
            and uuid4_text(task.get('task_id')) and task.get('status') in get_args(TaskStatus)
            and isinstance(task.get('steps'),list) and 1<=len(task['steps'])<=8):
        try:
            for key in ['created_at','updated_at']:
                datetime.fromisoformat(task[key])
            for step in task['steps']:
                if not (isinstance(step,dict) and isinstance(step.get('step_id'),str) and step['step_id']
                        and isinstance(step.get('tool_name'),str) and isinstance(step.get('purpose'),str)
                        and isinstance(step.get('arguments'),dict) and isinstance(step.get('depends_on'),list)
                        and all(isinstance(x,str) for x in step['depends_on']) and step.get('status') in get_args(StepStatus)):raise ValueError()
            if 'reference' in value and value['reference']!=task['task_id']:raise ValueError()
        except (KeyError,TypeError,ValueError):pass
        else:
            paths.append((('task','task_id'),task['task_id']))
            if 'reference' in value:paths.append((('reference',),task['task_id']))
    if (value.get('kind') in ('transfer','query') and positive_revision(value.get('revision'))
            and isinstance(value.get('reference'),str) and re.fullmatch('tg_[0-9a-f]{32}',value['reference'])
            and isinstance(value.get('fields'),dict)):
        if value.get('status')=='linked' and uuid4_text(value['fields'].get('task_id')):
            paths.append((('fields','task_id'),value['fields']['task_id']))
        if (value.get('status')=='preparing' and value.get('execution_outcome')=='unknown'
                and uuid4_text(value.get('verification_reference'))):paths.append((('verification_reference',),value['verification_reference']))
    if (set(value)=={'reference','kind','status','revision'} and value.get('kind')=='task'
            and value.get('status') in LIFECYCLE and positive_revision(value.get('revision')) and uuid4_text(value.get('reference'))):
        paths.append((('reference',),value['reference']))
    if (set(value)=={'reference','kind','scope'} and value.get('kind')=='task'
            and value.get('scope') in (focus_scope,redact(focus_scope)) and uuid4_text(value.get('reference'))):
        paths.append((('reference',),value['reference']))
    if (value.get('protocol')=='tool-foundation-1' and value.get('status')=='commit_unknown'
            and value.get('code') in ('preparation_unresolved','tool_outcome_unknown')
            and isinstance(value.get('effects'),list) and isinstance(value.get('retry'),dict)
            and isinstance(value.get('data'),dict) and uuid4_text(value['data'].get('verification_reference'))):
        paths.append((('data','verification_reference'),value['data']['verification_reference']))
    paths.extend(_payment_task_paths(value))
    return paths


def restore_metadata(original,projected):
    for path,reference in metadata_paths(original):
        target=projected
        for key in path[:-1]:
            if isinstance(target,dict):target=target.get(key)
            elif isinstance(target,list) and isinstance(key,int) and 0<=key<len(target):target=target[key]
            else:target=None
        if isinstance(target,dict) and path[-1] in target:target[path[-1]]=reference
        elif isinstance(target,list) and isinstance(path[-1],int) and 0<=path[-1]<len(target):
            target[path[-1]]=reference
    return projected
