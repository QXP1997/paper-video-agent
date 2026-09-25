"""离线演示批次 2 调用边界；复用测试替身，不访问模型服务或执行 Shell。"""

import asyncio
import sys

from _common import PROJECT_ROOT

# 此离线示例显式复用仓库测试 Fixture，不再编写另一套 Fake Backend / Sandbox。
sys.path.insert(0, str(PROJECT_ROOT))

from tests.support.loop_runtime import LoopRuntime
from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.workspaces import stage
from qharness.loop.config import Role
from qharness.loop.models import StartStage
from qharness.loop.transitions import create_run
from qharness.model.models import ChatMessage, ChatResponse, Usage
from qharness.run import create_loop_services
from qharness.tools.base import ToolExecutionPolicy, ToolRuntimePolicy
from qharness.tools.builtin.run_command import create_run_command_tool


async def main():
    runtime = LoopRuntime(initialize=False, policy=ToolExecutionPolicy(
        defaults=ToolRuntimePolicy(max_result_chars=512)))
    backend = ScriptedModelBackend([ChatResponse(
        ChatMessage("assistant", runtime.state.todo_plan.model_dump_json()), "stop", Usage(10, 5, 15), "scripted", "response-1",
    )])
    try:
        runtime.registry.register(create_run_command_tool(runtime.context))
        services = create_loop_services(runtime.context, backend=backend, executor=runtime.executor,
            database_manager=runtime.database, config=runtime.config, contract=runtime.state.contract)
        planned = await services.model.call("planner-1", Role.TODO_PLANNER)
        print(f"Planner 输出已校验：{len(planned.output.todos)} 个 Todo")
        state = create_run(runtime.state.run_id, runtime.state.contract, planned.output, questions=runtime.state.questions)
        services.repository.install_state(state)
        services.repository.apply(StartStage(run_id=state.run_id, expected_version=state.version,
                                            plan=stage(), attempt_id="attempt-1"))
        first = await services.tools.execute("tool-1", "run_command", {"command": "offline fixture"})
        print(f"工具调用成功={first.success}；业务退出码={first.data['exit_code']}；摘要截断={first.truncated}")
        assert first.data["exit_code"] == 1 and first.truncated
        assert len(first.content) <= 512
        restored = create_loop_services(runtime.context, backend=backend, executor=runtime.executor,
            database_manager=runtime.database, config=runtime.config, contract=runtime.state.contract)
        cached = restored.tools.read_result("tool-1")
        assert cached == first and len(runtime.sandbox.requests) == 1
        print(f"重建服务后读取旧结果：完整 stdout={len(cached.data['stdout'])} 字符，实际执行仍为 1 次")
        print(f"预算账本：{restored.repository.snapshot()['budget']}")
        backend.assert_exhausted()
    finally:
        await backend.close()
        runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
