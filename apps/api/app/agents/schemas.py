"""Validated Clarifier and Planner outputs."""

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=300)


class QuestionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=2000)
    type: Literal["single_choice", "multi_choice", "free_text"]
    options: list[QuestionOption] = Field(default_factory=list, max_length=30)
    allow_decide_for_me: bool = True

    @model_validator(mode="after")
    def validate_choices(self) -> "QuestionItem":
        if self.type == "free_text" and self.options:
            raise ValueError("Free-text questions cannot include options.")
        if self.type != "free_text" and not self.options:
            raise ValueError("Choice questions need at least one option.")
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Question option IDs must be unique.")
        return self


class QuestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionItem] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_ids(self) -> "QuestionSet":
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("Question IDs must be unique.")
        return self


class PlanMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    depends_on: list[str] = Field(default_factory=list)


class PlanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_brief: str = Field(min_length=1, max_length=10_000)
    tech_stack: list[str] = Field(min_length=1, max_length=20)
    milestones: list[PlanMilestone] = Field(min_length=1, max_length=30)
    tasks: list[PlanTask] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_task_graph(self) -> "PlanDocument":
        task_ids = {task.id for task in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique.")
        graph: dict[str, list[str]] = defaultdict(list)
        indegree = dict.fromkeys(task_ids, 0)
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency not in task_ids:
                    raise ValueError(f"Unknown task dependency: {dependency}")
                graph[dependency].append(task.id)
                indegree[task.id] += 1
        ready = [task_id for task_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for dependent in graph[node]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if visited != len(task_ids):
            raise ValueError("Task dependencies must not contain a cycle.")
        return self
