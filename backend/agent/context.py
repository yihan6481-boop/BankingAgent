"""Small, serializable V0.8.1 conversation context.

This is intentionally not a transcript or an audit log.  It contains only
public identifiers and small summaries needed for deterministic reference
resolution.  Authentication material, confirmation phrases and model
reasoning are never stored here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectedObject(ContextModel):
    kind: Literal["recipient", "card"]
    object_id: int = Field(gt=0)
    display: str = Field(min_length=1, max_length=120)
    selected_at_turn: int = Field(ge=0)
    source: Literal["explicit_request", "disambiguation", "preview", "completed"]


class PendingSummary(ContextModel):
    kind: Literal["action", "workflow_step"]
    tool_name: str
    step_id: str | None = None
    public_arguments: dict[str, Any] = Field(default_factory=dict)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    revision: int = Field(ge=1)


class ExecutedSummary(ContextModel):
    tool_name: str
    public_arguments: dict[str, Any] = Field(default_factory=dict)
    result_reference: str | None = Field(default=None, max_length=120)
    completed_at_turn: int = Field(ge=0)


class ClarificationSummary(ContextModel):
    tool_name: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=120)
    # 澄清期间仍需继承的金额上限约束（可信状态，不是模型输出）。
    constraints: list[dict[str, Any]] = Field(default_factory=list, max_length=4)


class ConversationContext(ContextModel):
    version: Literal["0.8.1"] = "0.8.1"
    turn: int = 0
    original_requests: list[dict[str, str]] = Field(default_factory=list)
    original_request_date: str | None = None
    query_requirements: dict[str, Any] | None = None
    tool_facts: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    selected_objects: dict[str, SelectedObject] = Field(default_factory=dict)
    reference_candidates: list[SelectedObject] = Field(default_factory=list, max_length=8)
    pending: PendingSummary | None = None
    missing_fields: list[str] = Field(default_factory=list)
    executed_results: list[ExecutedSummary] = Field(default_factory=list, max_length=4)
    clarification: ClarificationSummary | None = None

    def reset_scope(self) -> None:
        self.original_requests = []
        self.original_request_date = None
        self.query_requirements = None
        self.selected_objects = {}
        self.reference_candidates = []
        self.pending = None
        self.missing_fields = []
        self.clarification = None
        self.tool_facts = []

    def next_turn(self) -> None:
        self.turn += 1
        # A selected object is a short-lived reference hint, not a permanent
        # user preference.  Eight user turns is enough for the supported flows.
        stale = [
            key for key, value in self.selected_objects.items()
            if self.turn - value.selected_at_turn > 8
        ]
        for key in stale:
            self.selected_objects.pop(key, None)
        self.reference_candidates = [
            value for value in self.reference_candidates
            if self.turn - value.selected_at_turn <= 8
        ]

    def select(self, kind: Literal["recipient", "card"], object_id: int,
               display: str, source: str = "preview") -> None:
        selected = SelectedObject(
            kind=kind,
            object_id=object_id,
            display=display,
            selected_at_turn=self.turn,
            source=source,
        )
        self.selected_objects[kind] = selected
        self.reference_candidates = [
            item for item in self.reference_candidates
            if not (item.kind == kind and item.object_id == object_id)
        ]
        self.reference_candidates.append(selected)
        self.reference_candidates = self.reference_candidates[-8:]

    def record_executed(self, tool_name: str, arguments: dict[str, Any],
                        result_reference: str | None = None) -> None:
        self.executed_results.append(
            ExecutedSummary(
                tool_name=tool_name,
                public_arguments=dict(arguments),
                result_reference=result_reference,
                completed_at_turn=self.turn,
            )
        )
        self.executed_results = self.executed_results[-4:]

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


__all__ = [
    "ClarificationSummary",
    "ConversationContext",
    "ExecutedSummary",
    "PendingSummary",
    "SelectedObject",
]
