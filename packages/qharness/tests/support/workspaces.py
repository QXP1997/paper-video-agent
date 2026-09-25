"""固定分页任务及受控工具 Handler；执行限制仍交给已有 ToolExecutor。"""

from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from qharness.loop import Criterion, Question, StageKind, StagePlan, TaskContract, Todo, TodoPlan, create_run
from qharness.tools.base import Tool, ToolParameters
from qharness.workspace import WorkspaceContext


def pagination_run():
    contract = TaskContract(
        task_id="pagination", objective="修复分页并处理非法输入",
        criteria=(Criterion(id="C1", description="第一页及尾页结果正确，无重复或遗漏"),
                  Criterion(id="C2", description="页号及页大小非正数时抛出 ValueError")),
    )
    plan = TodoPlan(todos=(
        Todo(id="T1", objective="修复分页边界", acceptance_refs=("C1",), done_when=("边界回归通过",)),
        Todo(id="T2", objective="补齐输入校验", acceptance_refs=("C2",), done_when=("非法输入回归通过",)),
    ))
    return create_run("run-pagination", contract, plan, questions=(
        Question(id="Q1", description="偏移量是否多加了一页？", acceptance_refs=("C1",)),
    ))


def stage(todo_id="T1", stage_id="S1", *, kind=StageKind.INVESTIGATE, version=1, todo_version=1):
    return StagePlan(
        stage_id=stage_id, plan_version=version, todo_id=todo_id, todo_version=todo_version,
        kind=kind, objective="复现并定位偏移量问题" if kind == StageKind.INVESTIGATE else "完成并验证分页修复",
        addresses=("Q1",) if kind == StageKind.INVESTIGATE and todo_id == "T1" else
                  (("C1",) if todo_id == "T1" else ("C2",)),
        expected_results=("得到可检查的阶段结果",), stop_when=("已产出预期结果或遇到阻塞",),
        replan_when=("关键前提被观察推翻",),
    )


@contextmanager
def pagination_workspace() -> Iterator[WorkspaceContext]:
    with TemporaryDirectory(prefix="qharness-loop-test-") as directory:
        root = Path(directory)
        (root / "pagination.py").write_text(
            "def paginate(items, page, size):\n"
            "    start = page * size\n"
            "    return items[start:start + size]\n", encoding="utf-8",
        )
        yield WorkspaceContext(root)


class NoArguments(ToolParameters):
    pass


class ScriptedTool:
    """控制 Handler 的结果/异常，工具协议、校验、计数不重新实现。"""

    def __init__(self, results: Iterable[Any]) -> None:
        self.results = deque(results)
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        if not self.results:
            raise AssertionError("工具脚本已耗尽")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def as_tool(self, name="fixture_check") -> Tool:
        return Tool(name=name, description="返回预设的检查结果", parameters=NoArguments, handler=self)
