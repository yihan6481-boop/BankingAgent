"""Query application: one verified query and immutable source reference."""
from datetime import datetime, timezone
from data_query import QueryBatch, QueryPlan, verified_execute
from .drafts import GoalStore, public
from .contracts import feedback

def execute(principal,value):
    batch=QueryBatch(plans=[QueryPlan.model_validate(value['fields'])])
    data=verified_execute(principal,batch)
    result=GoalStore().save(principal,'result',dict(query_reference=value['reference'],
        query_revision=value['revision'],request=batch.model_dump(mode='json'),result=data,
        produced_at=datetime.now(timezone.utc).isoformat(),source='verified_query'),status='snapshot')
    return feedback(data=public(result),effects=['result_reference_saved'],next_actions=['result.read'])
