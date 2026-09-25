"""单次调用贯通任务：初步 Todo → 动态阶段 → 行动 → 验证 → 修复/完成。

离线模型与检查输出使用既有测试 Fixture，文件、历史和执行账本真实落盘。
不访问模型服务、不执行 Shell；此示例验证推理控制流程而非真实模型效果。
"""

import argparse
import asyncio
import sys

from _common import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from tests.support.task_flow import answer, catalog, flow_runtime, successful_script
from tests.support.workspaces import pagination_run


async def main(integration_failure=False):
    script = [answer(pagination_run().todo_plan), *successful_script(integration_failure=integration_failure)]
    runtime = flow_runtime(script, initial_plan=False, integration_failure=integration_failure)
    try:
        # 完整任务只有这个调用，外部不需要手写 StartStage、验收或反馈事件。
        state = await runtime.services.executor.run(catalog(), questions=runtime.state.questions)
        for attempt in state.attempts:
            verdict = attempt.verdicts[-1]
            print(f"{attempt.plan.todo_id} / {attempt.plan.stage_id} v{attempt.plan.plan_version} "
                  f"attempt {attempt.attempt_number}: {attempt.plan.kind} → "
                  f"stage={verdict.stage_status}, todo={verdict.todo_status} → {attempt.decision.route}")
        print("Task 验证轨迹：", [verdict.status.value for verdict in state.task_verdicts])
        print("最终状态：", state.phase.value)
        print("共享预算：", runtime.repository.snapshot()["budget"])
        runtime.backend.assert_exhausted()
        assert state.phase == "completed"
    finally:
        await runtime.backend.close()
        runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration-failure", action="store_true", help="最终集成检查首次失败，Executor 继续修复后完成")
    asyncio.run(main(parser.parse_args().integration_failure))
