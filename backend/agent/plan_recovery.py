"""One deterministic dependency correction; all other validators still run."""
from .schemas import PendingClarification


def repair_before_binding(proposal,flow,session,validator):
    from .flow_requirements import check
    original=check(flow,[(s.tool_name,s.arguments) for s in proposal.steps],proposal.steps,verify_bills=False)
    if not original or flow.get('ordering')!='explicit' or original[1]!='步骤顺序没有保留用户要求的前后依赖。':
        return proposal
    candidate=proposal.model_copy(deep=True)
    for i,s in enumerate(candidate.steps):
        if i and candidate.steps[i-1].step_id not in s.depends_on:s.depends_on.append(candidate.steps[i-1].step_id)
    session.plan_recovery=dict(attempts=1,diagnostic=dict(code=original[0],message=original[1]))
    if check(flow,[(s.tool_name,s.arguments) for s in candidate.steps],candidate.steps,verify_bills=False):
        return proposal
    validator.validate(candidate)
    return candidate


def clarification(session,fields,message):
    from .schemas import AgentReply
    session.pending_clarification=PendingClarification(kind='workflow_plan',tool_name='create_task_plan',
        missing_fields=fields,requires_full_replan=True)
    session.add('assistant',message)
    return AgentReply(action='clarification',message=message,tool_name='create_task_plan',
        clarification_scope='workflow_plan')


def prepare(proposal,session,validator):
    from .workflow import PlanValidationError
    flow=(session.context.query_requirements or {}).get('flow')
    if not flow:return proposal
    if not getattr(session,'plan_recovery',None):
        session.plan_recovery=dict(attempts=0,diagnostic=None)
    try:
        session.validate_legacy_plan(proposal)
        validator.validate(proposal)
        return proposal
    except PlanValidationError as error:
        session.plan_recovery['diagnostic']=dict(code=error.code,message=error.safe_message)
        # Only an independently proved ordering permits modifying candidate edges.
        # Parameters, goals and conditions are never repaired from model output.
        if flow.get('ordering')!='explicit' or error.safe_message!='步骤顺序没有保留用户要求的前后依赖。':
            raise
        candidate=proposal.model_copy(deep=True)
        for i,s in enumerate(candidate.steps):
            if i and candidate.steps[i-1].step_id not in s.depends_on:
                s.depends_on.append(candidate.steps[i-1].step_id)
        session.plan_recovery['attempts']=1
        session.validate_legacy_plan(candidate)
        validator.validate(candidate)
        return candidate
