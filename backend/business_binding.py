"""NL_COMPAT_01: bounded bindings over the existing PG04 request grammar.

No execution, model spans or new workflow language. Each user phase is consumed
once; existing wealth/condition validation owns action parameters and ordering.
"""
from dataclasses import dataclass

from agent.wealth_requirements import extract, check
from storage.config import PG04_WEALTH_TOOLS
from billvision.provider import VisionError


@dataclass(frozen=True)
class StepIntentBinding:
    step_id: str
    request: str


def bind_reads(text, plan, registry, today, previous=None):
    from agent.workflow import PlanValidator, PlanValidationError
    from agent.flow_requirements import extract as flow_extract, check as flow_check
    flow=flow_extract(text,today.isoformat(),(previous or {}).get('flow'))
    if flow is not None:
        try:
            PlanValidator(registry).validate(plan)
        except PlanValidationError as exc:
            raise VisionError(exc.code,exc.safe_message,422) from exc
        failure=flow_check(flow,[(s.tool_name,s.arguments) for s in plan.steps],plan.steps,verify_bills=False)
        if failure:raise VisionError(*failure,422)
        return [StepIntentBinding(s.step_id,g.get('request','')) for s,g in zip(plan.steps,flow['goals'])
                if g['kind']=='bill']
    wanted, _ = extract(text, bound_date=today.isoformat())
    if wanted is None:
        return None  # Other protocols retain their existing validation order.
    # Validate schema, whitelist, budgets, cycles, dependencies and condition
    # fields before attempting to match user phrases to candidate nodes.
    try:
        PlanValidator(registry).validate(plan)
    except PlanValidationError as exc:
        raise VisionError(exc.code, exc.safe_message, 422) from exc
    failure = check(wanted, [(s.tool_name,s.arguments) for s in plan.steps], plan.steps)
    if failure:
        raise VisionError(*failure, 422)
    # Equal month/card/metric does not identify the right observation phase.
    # In this bounded grammar a condition creates its own immediately prior
    # read phase; an earlier identical query may predate an intervening write.
    for index,step in enumerate(plan.steps):
        if step.condition and (index==0 or step.condition.source_step_id!=plan.steps[index-1].step_id):
            raise VisionError('query_scope_mismatch','条件未引用对应阶段的查询结果，不能借用较早的同范围结果；本次未执行。',422)
    others=[s for s in plan.steps if s.tool_name not in PG04_WEALTH_TOOLS]
    if len(others)!=len(wanted['base_parts']):
        raise VisionError('query_scope_mismatch','计划遗漏目标或添加了未请求的步骤，本次未执行。',422)
    return [StepIntentBinding(s.step_id, fragment) for s,fragment in zip(others,wanted['base_parts'])]
