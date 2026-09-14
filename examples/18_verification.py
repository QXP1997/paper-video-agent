"""离线演示三层真实控制流程；检查输出复用可控 Sandbox Fixture，不运行 Shell。"""

import argparse
import asyncio
import sys

from _common import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from tests.support.loop_runtime import LoopRuntime
from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.workspaces import stage
from qharness.loop import ApplyFeedback, FeedbackDecision, StageOutcome, StartStage, SubmitOutcome
from qharness.run import create_loop_services
from qharness.tools.builtin.run_command import create_run_command_tool
from qharness.verification import CheckSpec


async def main(integration_failure=False):
    runtime = LoopRuntime()
    backend = ScriptedModelBackend([])
    try:
        runtime.registry.register(create_run_command_tool(runtime.context))
        runtime.context.metadata["verification_environment"] = "offline-fixture-v1"
        runtime.sandbox.exit_code = 0
        runtime.sandbox.stdout = "Ran 3 tests in 0.01s\nOK\n"
        (runtime.context.workspace.root / "pagination.py").write_text("# offline subject\n", encoding="utf-8")
        services = create_loop_services(runtime.context, backend=backend, executor=runtime.executor,
            database_manager=runtime.database, config=runtime.config, contract=runtime.state.contract)
        for number, todo in enumerate(runtime.state.todo_plan.todos, 1):
            state = services.repository.snapshot()["state"]
            plan = stage(todo.id, f"S{number}", kind="validate")
            state = services.repository.apply(StartStage(run_id=state.run_id, expected_version=state.version,
                                                        plan=plan, attempt_id=f"A{number}"))
            # 演示的 Actor 产出是脚本替身；Verifier 的运行与状态提交是真实实现。
            services.repository.apply(SubmitOutcome(run_id=state.run_id, expected_version=state.version,
                outcome=StageOutcome(**state.attempts[-1].identity.model_dump(), status="candidate",
                                     summary="待独立检查的候选结果")))
            common = dict(command="python -m unittest", kind="unittest", inputs=("pagination.py",))
            verdict = await services.verifier.verify_stage(f"verify-{number}", [
                CheckSpec(id=f"stage-{number}", scope="stage", targets=plan.expected_results, **common),
                CheckSpec(id=f"todo-{number}", scope="todo", targets=(*todo.acceptance_refs, *todo.done_when), **common),
            ])
            print(f"{todo.id}: stage={verdict.stage_status}, todo={verdict.todo_status}")
            state = services.repository.snapshot()["state"]
            services.repository.apply(ApplyFeedback(run_id=state.run_id, expected_version=state.version,
                decision=FeedbackDecision(route="advance", reason="阶段与 Todo 分别通过检查")))
        print("全部 Todo 通过后的状态：", services.repository.snapshot()["state"].phase)
        if integration_failure:
            runtime.sandbox.exit_code = 1
            runtime.sandbox.stdout = "FAIL: test_integration\nRan 3 tests in 0.01s\nFAILED (failures=1)\n"
        verdict = await services.verifier.verify_task("final", [CheckSpec(id="integration", scope="task",
            targets=tuple(c.id for c in runtime.state.contract.criteria), **common)])
        state = services.repository.snapshot()["state"]
        print(f"独立 Task 验证：{verdict.status}；最终状态：{state.phase}")
        print("Todo 状态：", {t.todo.id: t.status.value for t in state.todos})
        print("预算：", services.repository.snapshot()["budget"])
        report = services.repository.read_artifact(services.verifier.report_ref("final"))
        print("Failure Bundle 数量：", len(report["failures"]))
    finally:
        await backend.close()
        runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration-failure", action="store_true", help="展示最终集成失败后重开 Todo")
    asyncio.run(main(parser.parse_args().integration_failure))
