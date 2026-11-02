"""Controlled natural references to the existing task lifecycle service.

No LLM authorization and no business writes here. Every transition goes through
TaskStore CAS/lease, the existing service, and authenticated server identity.
"""
import re
from datetime import timedelta
from .models import RevisionRequest, ModifyRequest, ScheduleRequest
from .store import TaskStore, TaskError, digest
from .service import transition, public

COMMAND = re.compile(r'^(?:(?:请|帮我|先|等等|等一下)[，,\s]*)*(暂停|继续|恢复|取消|终止|手动接管|接管|交回)(.*?)[。！!]?\s*$')
REFERENCE = re.compile(r'^(?:这个任务|当前任务|刚才的任务|刚才那个任务|刚才那个|刚才的|这个|任务|整个任务|未完成任务|转账任务|任务\s*[1-9][0-9]*)?$')
MODIFY = re.compile(r'^(?:(?:请|帮我|等等|等一下)[，,\s]*)?(?:(?:把)?(?:刚才|当前)(?:那笔|的)?(?:转账|任务|操作)?(?:从[0-9.]+(?:元)?)?)?改(?:成|为)?(?:从([1-9][0-9]*)号卡转(?:账)?)?\s*([0-9]+(?:\.[0-9]{1,2})?)?(?:元)?[。！!]?\s*$')
STATUS={'active':'正在执行','waiting':'等待补充信息、确认或独立验证','paused':'已暂停','scheduled':'已预约',
        'manual':'已接管','completed':'已完成','cancelled':'已取消','failed':'已停止，存在失败步骤'}
STEP={'completed':'已核验完成','pending':'尚未执行','running':'执行中','waiting_confirmation':'等待确认',
      'waiting_verification':'等待独立验证','waiting_clarification':'等待补充信息','skipped':'条件不满足，已跳过',
      'failed':'失败','cancelled':'已取消','expired':'已过期'}


def summary(record):
    # Facts come only from the persisted state. A preview can never say committed.
    counts={}
    for s in record.task.steps:counts[s.status]=counts.get(s.status,0)+1
    details='；'.join(f'{STEP.get(k,"未完成")} {n} 步' for k,n in counts.items())
    return f'任务{STATUS[record.status]}。{details}。已提交业务及回执保留；新的资金步骤仍须逐项确认和必要验证。'


def handle(text,session,principal,mode):
    from api.secure_chat import SecureChatResponse
    from api.secure import task_response
    from api.runtime import runtime
    from security.service import get_security_services
    from conversation import task_event
    current=get_security_services().now_provider()
    text=text.strip()
    selected=re.fullmatch(r'(?:选择)?任务\s*([1-9][0-9]*)',text)
    pending=getattr(session,'task_reference_question',None)
    command=COMMAND.fullmatch(text)
    modification=MODIFY.fullmatch(text)
    card_change=re.fullmatch(r'换(?:成|用)?(?:从)?([1-9][0-9]*)号卡[。！!]?',text)
    action=None;changes={}
    scheduled=re.fullmatch(r'(?:请|把)?(?:这个|当前|刚才的)?任务预约到\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))',text)
    if command and REFERENCE.fullmatch(command[2].strip()):
        action={'暂停':'pause','继续':'resume','恢复':'resume','取消':'cancel','终止':'cancel',
                '手动接管':'takeover','接管':'takeover','交回':'return'}[command[1]]
    elif modification and (modification[1] or modification[2]):
        action='modify'
        if modification[1]:changes['from_card_id']=int(modification[1])
        if modification[2]:changes['amount']=modification[2]
    elif card_change:
        action='modify';changes={'from_card_id':int(card_change[1])}
    elif selected and pending:
        if current>=pending['expires']:
            session.task_reference_question=None
            return SecureChatResponse(action='clarification',message='任务选择已过期，请重新说明要操作哪个任务。')
        action=pending['action'];changes=pending['changes']
    elif scheduled:
        action='schedule';changes={'at':scheduled[1]}
    else:
        return None
    if mode!='action':
        return SecureChatResponse(action='error',error_code='explicit_action_required',
            message='任务操作请先切换到办理模式；查询模式不会修改或推进资金任务。')
    candidates=[r for r in TaskStore().list(principal) if r.status not in {'completed','cancelled'}]
    # An unqualified reference never reaches a different login/session. A user
    # can deliberately select an owned durable task through the existing panel.
    candidates=[r for r in candidates if r.origin_session_hash==digest(session.session_id) or
        r.task.task_id==session.lifecycle_id]
    if selected and pending:
        number=int(selected[1])-1
        if number>=len(pending['candidates']):
            return SecureChatResponse(action='clarification',message='请选择列表中存在的任务编号。')
        tid,revision=pending['candidates'][number]
        candidates=[r for r in candidates if r.task.task_id==tid and r.revision==revision]
        if not candidates:
            session.task_reference_question=None
            return SecureChatResponse(action='clarification',message='候选任务已变化，请重新说明要操作的任务。')
    elif command and (number:=re.fullmatch(r'任务\s*([1-9][0-9]*)',command[2].strip())):
        # Numbers only have meaning in the current, expiring server question.
        return SecureChatResponse(action='clarification',message='请先在任务面板选择任务，或重新提出操作以获取当前候选列表。')
    if len(candidates)!=1:
        if not candidates:
            if command and not command[2].strip() and not session.lifecycle_id:
                return None  # Existing read-only dialogue cancellation/continuation.
            return SecureChatResponse(action='clarification',message='当前会话没有唯一的未完成任务；历史任务请先在任务面板明确选择并恢复。')
        session.task_reference_question=dict(action=action,changes=changes,expires=current+timedelta(minutes=3),
            candidates=[(r.task.task_id,r.revision) for r in candidates])
        choices='；'.join(f'任务{i+1}：{STATUS[r.status]}，{len(r.task.steps)}步' for i,r in enumerate(candidates))
        return SecureChatResponse(action='clarification',message='存在多个未完成任务，请回复“选择任务1”等明确编号。'+choices)
    record=candidates[0]
    session.task_reference_question=None
    try:
        if action=='modify':
            targets=[s for s in record.task.steps if s.status not in {'completed','cancelled','skipped'} and
                s.tool_name in {'transfer_money','buy_wealth_product','redeem_wealth_product'}]
            if len(targets)!=1:
                return SecureChatResponse(action='clarification',message='无法唯一确定要修改的未执行资金步骤，请在任务面板选择具体步骤。')
            target=targets[0]
            if 'from_card_id' in changes and target.tool_name=='buy_wealth_product':
                changes['card_id']=changes.pop('from_card_id')
            body=ModifyRequest(revision=record.revision,step_id=target.step_id,changes=changes)
        elif action=='schedule':body=ScheduleRequest(revision=record.revision,at=changes['at'])
        else:body=RevisionRequest(revision=record.revision)
        record=transition(session,record.task.task_id,body,action)
        # Modification never confirms. Re-preview only the unique modified task,
        # and keep a manually controlled task under manual control.
        if action=='modify' and record.status!='manual':
            record=transition(session,record.task.task_id,RevisionRequest(revision=record.revision),'resume')
        task_event(session,record,action)
        runtime.set_last_reply(session.session_id,None)
        return SecureChatResponse(action='confirmation_required' if record.status=='waiting' and session.pending_action else 'task_result',
            message=summary(record),task=task_response(record,session if record.status!='failed' else None))
    except TaskError as exc:
        if action=='modify' and exc.code=='constraint_conflict':
            from agent.agent import BankingAgent
            BankingAgent._set_constraint_draft(session,tool_name=target.tool_name,
                arguments={**target.arguments,**changes},constraints=target.constraints,
                reason=exc.code,step_id=target.step_id,task_id=record.task.task_id)
        messages={'manual_task':'任务已被手动接管，请明确交回后再恢复。',
            'constraint_conflict':'修改超过当前任务的金额上限，请提供符合约束的金额；没有执行交易。',
            'stale_revision':'任务已变化，请同步后重试。','terminal_task':'任务已结束，不能修改已提交交易。',
            'completed_step_immutable':'已完成步骤不能修改。','reconciliation_required':'提交状态需要先核查原交易回执；不会重复提交。'}
        return SecureChatResponse(action='clarification' if exc.code=='constraint_conflict' else 'error',error_code=exc.code,
            message=messages.get(exc.code,'任务未能继续，请同步任务并核对当前状态；未额外授权资金操作。'))
