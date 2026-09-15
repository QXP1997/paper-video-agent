"""第七批离线演示：暂停恢复、追加约束、审批或结果确认中断后的核对。

模型/沙箱检查使用可控 Fixture；文件、Commit、输入、状态与调用账本真实落盘。
"""

import argparse
import asyncio
import sys
from unittest.mock import patch

from _common import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from qharness.exception import LoopExecutionError
from qharness.run import RunService
from qharness.tools.base import ToolExecutionPolicy, ToolPolicyOverride, ToolRuntimePolicy, ToolExecutionResult
from qharness.tools.hooks import ToolExecutionHook
from qharness.verification import CheckCatalog, CheckSpec
from tests.support.task_flow import catalog, flow_runtime, handed_back, planned, successful_script, write


async def main(mode):
    script = successful_script(investigate=False, repair=False)
    if mode == "steering":
        script = [planned(), write("pagination.py", "fixed"), planned(), handed_back(),
                  planned(), write("validation.py", "validated"), handed_back()]
    policy = ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100), tool_overrides=(
        {"write_file": ToolPolicyOverride(requires_approval=True)} if mode == "approval" else {}))
    runtime = flow_runtime(script, policy=policy)
    lifecycle = RunService(runtime.services)
    checks = catalog()
    try:
        if mode in ("pause", "steering"):
            class PauseOnce(ToolExecutionHook):
                paused = False
                async def after_execute(self, request, tool, result):
                    if tool.name == "write_file" and not self.paused:
                        self.paused = True
                        await lifecycle.submit("pause-1", "pause")
            runtime.executor.hooks.append(PauseOnce())
            waiting = await lifecycle.run(checks)
            print("暂停位置:", waiting.phase, waiting.resume_phase, waiting.active_attempt_id)
            print("已发生的工具执行:", runtime.repository.snapshot()["budget"]["tool_executions"])
            lifecycle = RunService(runtime.services)
            if mode == "steering":
                await lifecycle.submit("user-update", "steer", {"constraints": ["保留集成接线"]})
                checks = CheckCatalog((*checks.specs, CheckSpec(id="keep-integration", scope="task",
                    targets=("保留集成接线",), command="integration", kind="unittest",
                    inputs=("pagination.py", "integration.py"))))
            else:
                await lifecycle.submit("resume-1", "resume")
        elif mode == "approval":
            for i in range(2):
                waiting = await lifecycle.run(checks)
                assert waiting.phase == "waiting"
                ref = [item["payload"]["ref"] for item in runtime.repository.inputs("applied")
                       if item["kind"] == "approval_request"][-1]
                print("等待审批，执行次数:", runtime.repository.snapshot()["budget"]["tool_executions"])
                # 示例模拟应用已收到用户的明确允许；真实应用只能在认证/授权后调用。
                await lifecycle.submit(f"approval-{i}", "approval", {"approval_ref": ref, "allow": True})
        else:
            with patch.object(runtime.services.repository, "finish_tool",
                              side_effect=LoopExecutionError("模拟结果确认前中断", code="storage_error")):
                try:
                    await lifecycle.run(checks)
                except LoopExecutionError as error:
                    assert error.code == "storage_error"
            unknown = next(item for item in lifecycle.recovery() if item["kind"] == "tool" and item["status"] == "dispatched")
            print("需要核对:", unknown)
            # 此 Fixture 的同步写入已结束；用同一个 operation 的真实 Commit 和 Diff 核对。
            mutation = runtime.context.mutation_service.inspect(unknown["operation_id"])
            assert mutation.status == "applied"
            proof = await lifecycle.recovery_controller.observe("tool", unknown["call_id"], resolution="completed",
                description="已检查原操作 Commit 和当前文件，执行者已停止", quiescent=True,
                result=ToolExecutionResult(unknown["call_id"], "write_file", True, "已核对文件写入", 0, data=mutation.to_dict()))
            await lifecycle.recovery_controller.reconcile(proof)
        result = await lifecycle.run(checks)
        print("最终状态:", result.phase, "契约版本:", result.contract.version)
        print("共享预算:", runtime.repository.snapshot()["budget"])
        print("事件数:", len(lifecycle.events(limit=1000)))
        runtime.backend.assert_exhausted()
        assert result.phase == "completed"
    finally:
        runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pause", "steering", "approval", "recovery"), default="pause")
    asyncio.run(main(parser.parse_args().mode))
