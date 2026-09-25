from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .investigation import EvidenceRecord
from .trace import TraceWriter


@dataclass(frozen=True)
class TaskMessage:
    case_id: str
    task_id: str
    sender: str
    recipient: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ResultMessage:
    case_id: str
    task_id: str
    sender: str
    recipient: str
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...]


class A2AProtocol:
    """Case-scoped, one-way task handoffs with observable trace events."""

    def __init__(self, case_id: str, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.trace = trace
        self._counter = 0
        self._open: set[str] = set()

    def assign(self, recipient: str, payload: dict[str, Any] | None = None) -> TaskMessage:
        self._counter += 1
        task = TaskMessage(
            case_id=self.case_id,
            task_id=f"{self.case_id}:{self._counter}",
            sender="coordinator",
            recipient=recipient,
            payload=payload or {},
        )
        self._open.add(task.task_id)
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=task.sender,
            target=recipient,
            attributes={"task_id": task.task_id},
        )
        return task

    def complete(
        self,
        task: TaskMessage,
        payload: dict[str, Any],
        evidence: list[EvidenceRecord] | None = None,
    ) -> ResultMessage:
        if task.case_id != self.case_id or task.task_id not in self._open:
            raise ValueError("A2A task is outside this case or already completed")
        refs_by_tool: dict[str, list[str]] = {}
        for record in evidence or []:
            if record.case_id != self.case_id:
                raise ValueError("A2A handoff contains cross-case evidence")
            refs_by_tool.setdefault(record.tool_name, []).append(record.evidence_ref)
        self._open.remove(task.task_id)
        for tool_name, refs in refs_by_tool.items():
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=task.recipient,
                tool_name=tool_name,
                evidence_refs=list(dict.fromkeys(refs)),
                attributes={"task_id": task.task_id},
            )
        result = ResultMessage(
            case_id=self.case_id,
            task_id=task.task_id,
            sender=task.recipient,
            recipient="coordinator",
            payload=payload,
            evidence_refs=tuple(
                dict.fromkeys(ref for refs in refs_by_tool.values() for ref in refs)
            ),
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=result.sender,
            target=result.recipient,
            attributes={"task_id": task.task_id, "status": "completed"},
        )
        return result

    def assert_complete(self) -> None:
        if self._open:
            raise ValueError(f"unfinished A2A tasks: {sorted(self._open)}")
