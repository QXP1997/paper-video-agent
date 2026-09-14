"""固定分页任务：模型脚本 + 文件工具真实落盘 + 可控沙箱检查。"""

import json

from qharness.loop import RunState, StagePlan
from qharness.model.models import ChatMessage, ChatResponse, FunctionCall, ToolCall, Usage
from qharness.run import create_loop_services
from qharness.tools.base import ToolExecutionPolicy, ToolRuntimePolicy
from qharness.tools.builtin.run_command import create_run_command_tool
from qharness.tools.builtin.write_file import create_write_file_tool
from qharness.verification import CheckCatalog, CheckSpec, QuestionConclusion
from tests.loop.test_actor import handback
from tests.support.loop_runtime import LoopRuntime
from tests.support.scripted_backend import ScriptedModelBackend


def payload(request):
    return json.loads(request.messages[1].content)


def answer(value):
    content = value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value)
    return ChatResponse(ChatMessage("assistant", content), "stop", Usage(1, 1, 2), "scripted", "response")


def planned(kind="implement"):
    def response(request):
        data = payload(request)
        state, todo = data["run_state"], data["current_todo"]["todo"]
        attempts = state["attempts"]
        replanning = state["pending_decision"] and state["pending_decision"]["route"] == "replan_stage"
        return answer(StagePlan(stage_id=attempts[-1]["plan"]["stage_id"] if replanning else f"S{len(attempts) + 1}",
            plan_version=attempts[-1]["plan"]["plan_version"] + 1 if replanning else 1,
            todo_id=todo["id"], todo_version=todo["version"], kind=kind, objective=todo["objective"],
            addresses=todo["acceptance_refs"], expected_results=("定位偏移错误",) if kind == "investigate" else
                (("分页结果正确",) if "C1" in todo["acceptance_refs"] else ("非法输入正确拒绝",)),
            stop_when=("已形成候选结果",), replan_when=("关键前提改变",)))
    return response


def handed_back(**changes):
    return lambda request: handback(RunState.model_validate(payload(request)["run_state"]), **changes)


def write(path, content):
    return ChatResponse(ChatMessage("assistant", tool_calls=[ToolCall("write", FunctionCall("write_file",
        json.dumps({"path": path, "content": content, "overwrite": True})))]), "tool_calls", Usage(1, 1, 2), "scripted", "response")


def catalog():
    def spec(id, scope, targets, command, inputs, **kwargs):
        return CheckSpec(id=id, scope=scope, targets=targets, command=command, kind="unittest", inputs=inputs, **kwargs)
    return CheckCatalog((
        spec("investigate", "stage", ("定位偏移错误",), "investigate", ("pagination.py",),
             conclusions=(QuestionConclusion(question_id="Q1", finding="分页偏移错误已复现"),)),
        spec("stage-boundary", "stage", ("分页结果正确",), "boundary", ("pagination.py",)),
        spec("todo-boundary", "todo", ("C1", "边界回归通过"), "boundary", ("pagination.py",)),
        spec("stage-input", "stage", ("非法输入正确拒绝",), "input", ("validation.py",)),
        spec("todo-input", "todo", ("C2", "非法输入回归通过"), "input", ("validation.py",)),
        spec("task-boundary", "task", ("C1",), "integration", ("pagination.py", "integration.py")),
        spec("task-input", "task", ("C2",), "input", ("validation.py",)),
    ))


def flow_runtime(script, *, config=None, initial_plan=True, integration_failure=False, state=None, policy=None):
    runtime = LoopRuntime(config=config, initialize=False, state=state,
        policy=policy or ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100)))
    runtime.database.initialize()
    root = runtime.context.workspace.root
    for name, text in (("pagination.py", "wrong"), ("validation.py", "missing"),
                       ("integration.py", "missing" if integration_failure else "wired")):
        (root / name).write_text(text, encoding="utf-8")
    runtime.context.metadata["verification_environment"] = "offline-task-checks-v1"
    runtime.registry.register(create_run_command_tool(runtime.context))
    runtime.registry.register(create_write_file_tool(runtime.attach_history()))
    def check(request):
        name = request.command
        content = lambda path: (root / path).read_text(encoding="utf-8")
        passed = {"investigate": True, "boundary": content("pagination.py") == "fixed",
                  "input": content("validation.py") == "validated",
                  "integration": content("pagination.py") == "fixed" and content("integration.py") == "wired"}[name]
        runtime.sandbox.exit_code = 0 if passed else 1
        runtime.sandbox.stdout = ("Ran 2 tests in 0.01s\nOK\n" if passed else
                                 "FAIL: test_" + name + "\nRan 2 tests in 0.01s\nFAILED (failures=1)\n")
    runtime.sandbox.on_execute = check
    runtime.backend = ScriptedModelBackend(script)
    runtime.services = create_loop_services(runtime.context, backend=runtime.backend, executor=runtime.executor,
        database_manager=runtime.database, config=runtime.config, contract=runtime.state.contract,
        state=runtime.state if initial_plan else None)
    return runtime


def successful_script(*, investigate=True, repair=True, integration_failure=False):
    steps = []
    if investigate:
        steps.extend([planned("investigate"), handed_back()])
    steps.extend([planned(), write("pagination.py", "still wrong" if repair else "fixed"), handed_back()])
    if repair:
        steps.extend([write("pagination.py", "fixed"), handed_back()])
    steps.extend([planned(), write("validation.py", "validated"), handed_back()])
    if integration_failure:
        steps.extend([planned(), write("integration.py", "wired"), handed_back()])
    return steps
