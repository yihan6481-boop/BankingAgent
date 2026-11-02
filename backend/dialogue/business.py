"""Connect registered structured services; no intent parsing or business state copies."""
from uuid import uuid4
from pydantic import ValidationError
from foundation.contracts import feedback, classify
from lifecycle.store import TaskError, TaskStore
from lifecycle.service import registry, public
from lifecycle.application import prepare_structured, perform
from lifecycle.models import (CreateTaskRequest, RevisionRequest, ScheduleRequest,
                              NoteRequest, RollbackRequest)
from execution.sessions import session_for
from agent.tool_registry import ToolRisk

WRITES={'report_card_lost','set_daily_limit','cancel_subscription','buy_wealth_product','redeem_wealth_product'}
READS={'get_monthly_spending','get_transactions_by_month','get_category_spending',
       'get_active_subscriptions','get_monthly_subscription_cost','get_risk_profile',
       'get_wealth_products','get_wealth_holdings','query_financial_records',
       'get_monthly_cash_flow_report','get_annual_cash_flow_report','get_expense_clues',
       'compare_wealth_products','explain_wealth_suitability','query_data','query_uploaded_bills'}
ALLOWED=READS|WRITES|{'get_cards','get_transfer_recipients','transfer_money','query_data','query_uploaded_bills'}
ACTIONS={'task_schedule':ScheduleRequest,'task_takeover':RevisionRequest,
         'task_return':RevisionRequest,'task_note':NoteRequest,'task_rollback':RollbackRequest}


def strict_ids(value):
    # Legacy ID models coerce bools; at the model boundary they are never IDs.
    if isinstance(value,dict):
        for key,v in value.items():
            if key.endswith('_id') and isinstance(v,bool):raise TaskError('invalid_identifier')
            strict_ids(v)
    elif isinstance(value,list):
        for v in value:strict_ids(v)


def prepare(plan,p,sid):
    from .links import binding
    from .store import DialogueStore
    from .projection import bounded_result
    body=CreateTaskRequest(plan=plan)
    if any(s.tool_name not in ALLOWED for s in body.plan.steps):raise TaskError('r2_business_out_of_scope')
    # Validate before reserving/calling services; no new business draft is made.
    from lifecycle.service import engine
    engine().validator.validate(body.plan)
    scope=binding.get();reserved=str(uuid4())
    if scope:reserved=DialogueStore().reserve_preparation(scope[1],scope[2])
    try:data=prepare_structured(sid,body,p,preparation_task_id=reserved)
    except Exception as exc:
        if getattr(exc,'preparation_no_task',False):raise
        try:record=TaskStore().load(reserved,p)
        except TaskError as missing:
            if missing.code!='task_not_found':raise
            return feedback('commit_unknown','preparation_unresolved',data=dict(verification_reference=reserved,
                task_created=None),next_actions=['task_verify'])
        data={'task':public(record)}
    return bounded_result(feedback(data=data,effects=['task_prepared'],next_actions=['task_read','secure confirmation protocol']))


def execute(name,args,p,sid):
    from .projection import bounded_result
    tools=registry()
    if name in READS:
        from agent.tool_router import ToolRouter
        dispatch=ToolRouter(tools).dispatch(name,args,principal=p,session=session_for(sid))
        result=dispatch.result
        if result is None:raise TaskError('invalid_read_result')
        if not result.success:
            code=result.error_code or 'service_unavailable'
            return feedback('denied' if code=='query_authorization_required' else classify(code),code,
                data={'message':result.message},next_actions=['existing UI query authorization'] if code=='query_authorization_required' else [])
        from datetime import datetime,timezone
        return feedback(data=dict(result.model_dump(mode='json'),queried_at=datetime.now(timezone.utc).isoformat(),historical=True))
    if name in WRITES:
        spec=tools.get(name)
        if name=='cancel_subscription' and not any(args.get(k) is not None for k in ['subscription','subscription_id']):
            return feedback('missing_information','parameters_required',data=dict(missing_fields=['subscription_or_subscription_id'],scope='no business draft saved'))
        try:validated=spec.argument_model.model_validate(args)
        except ValidationError as exc:
            errors=exc.errors();missing=['.'.join(map(str,e['loc'])) for e in errors if e['type']=='missing']
            if missing:return feedback('missing_information','parameters_required',data=dict(missing_fields=missing,
                scope='no business draft saved; collect parameters in conversation'))
            raise
        from agent.workflow import TOOL_DOMAINS
        from agent.schemas import TaskPlanProposal
        plan=TaskPlanProposal.model_validate(dict(contract=dict(contract_version='1.0',goal=spec.description,
            domains=[TOOL_DOMAINS[name]],requested_tools=[name],constraints=[]),steps=[dict(step_id='operation',purpose=spec.description,
                tool_name=name,arguments=validated.model_dump(mode='json'),depends_on=[])]))
        return prepare(plan,p,sid)
    if name=='create_task_plan':
        from agent.schemas import TaskPlanProposal
        return prepare(TaskPlanProposal.model_validate(args),p,sid)
    if name in ACTIONS:
        values=dict(args);ref=values.pop('reference')
        from .tools import task_allowed
        task_allowed(p,ref)
        return bounded_result(feedback(data=perform(sid,ref,ACTIONS[name].model_validate(values),name.split('_',1)[1],p),effects=['task_transition']))
    raise TaskError('unsupported_operation')
