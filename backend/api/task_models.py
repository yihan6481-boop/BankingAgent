from agent.schemas import StrictModel
from pydantic import model_validator
from lifecycle.models import TaskView
class TaskResponse(StrictModel):
    task: TaskView
    interaction: 'V2Envelope | None' = None

    @model_validator(mode='after')
    def analysis_contract(self):
        from analysis_service import CATALOG, ADAPTER
        for step in self.task.task.steps:
            if step.tool_name in CATALOG and step.result and step.result.success:
                ADAPTER.validate_python(step.result.data)
        return self


class TaskListResponse(StrictModel):
    tasks: list[TaskView]


from api.v2_schemas import V2Envelope
TaskResponse.model_rebuild()
