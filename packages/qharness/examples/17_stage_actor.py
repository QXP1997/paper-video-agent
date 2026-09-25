"""使用已配置的真实模型和 SRT 沙箱，按阶段目标自动读文件、修复分页并运行回归。

需要 model/database/history/sandbox/tool 配置及可用的沙箱。不会自动进行系统初始化。
每次运行使用新的演示工作区，结果保留供检查。--stream 使用 Backend 的完整流聚合。
"""

import argparse
import asyncio
import json
import uuid

from _common import (DATABASE_CONFIG_PATH, HISTORY_CONFIG_PATH, PROJECT_ROOT,
                     SANDBOX_CONFIG_PATH, SANDBOX_WORKSPACE_ROOT, TOOL_CONFIG_PATH, create_backend)
from qharness.loop.config import load_loop_config
from qharness.loop.models import Criterion, StagePlan, TaskContract, Todo, TodoPlan
from qharness.loop.transitions import create_run
from qharness.persistence import DatabaseManager, load_database_config
from qharness.run import create_loop_services, create_run_context
from qharness.sandbox.config import load_sandbox_config
from qharness.tools import (BuiltinToolProvider, FileMutationToolProvider, SandboxToolProvider,
                           ToolExecutor, ToolRegistry, load_tool_policy, load_tool_providers)
from qharness.workspace import load_workspace_history_config


async def main(stream=False):
    run_id = "stage-demo-" + uuid.uuid4().hex
    workspace = SANDBOX_WORKSPACE_ROOT / run_id
    workspace.mkdir(parents=True)
    (workspace / "pagination.py").write_text(
        "def paginate(items, page, size):\n    start = page * size\n    return items[start:start + size]\n", encoding="utf-8")
    (workspace / "test_pagination.py").write_text(
        "from pagination import paginate\n"
        "assert paginate(list(range(5)), 1, 2) == [0, 1]\n"
        "assert paginate(list(range(5)), 2, 2) == [2, 3]\n"
        "assert paginate(list(range(5)), 3, 2) == [4]\n"
        "print('pagination checks passed')\n", encoding="utf-8")
    database = DatabaseManager(load_database_config(DATABASE_CONFIG_PATH))
    backend = None
    try:
        context = create_run_context(tenant_id="local-demo", workspace_id=run_id, run_id=run_id,
            workspace_root=workspace, sandbox_config=load_sandbox_config(SANDBOX_CONFIG_PATH),
            database_manager=database, history_config=load_workspace_history_config(HISTORY_CONFIG_PATH))
        status = await context.sandbox.check_status()
        if not status.available:
            raise RuntimeError("沙箱尚不可用，请先完成原有沙箱准备流程：" + status.message)
        registry = ToolRegistry()
        await load_tool_providers(registry, [BuiltinToolProvider(context.workspace),
            FileMutationToolProvider(context.mutation_service), SandboxToolProvider(context)])
        executor = ToolExecutor(registry, policy=load_tool_policy(TOOL_CONFIG_PATH))
        backend = create_backend()
        contract = TaskContract(task_id=run_id, objective="修复分页的起始偏移量，不修改测试文件",
            criteria=(Criterion(id="C1", description="第一页、中间页、尾页均返回正确项目"),),
            constraints=("不得修改 test_pagination.py 的测试要求",))
        todos = TodoPlan(todos=(Todo(id="T1", objective="修复分页边界", acceptance_refs=("C1",),
                                    done_when=("原分页回归脚本通过",)),))
        state = create_run(run_id, contract, todos)
        plan = StagePlan(stage_id="S1", todo_id="T1", todo_version=1, kind="implement",
            objective="读取相关文件、修复偏移量，并通过 run_command 执行 python test_pagination.py 检查",
            addresses=("C1",), expected_results=("pagination.py 修复，原回归脚本正常退出",),
            stop_when=("原分页回归检查以零退出码完成",),
            replan_when=("发现问题需要改变分页接口或测试要求，当前阶段前提不成立",))
        services = create_loop_services(context, backend=backend, executor=executor, database_manager=database,
            config=load_loop_config(PROJECT_ROOT / "config/loop.example.toml"), contract=contract, state=state)
        result = await services.actor.run(plan, attempt_id="attempt-1", stream=stream)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        print(f"工作区：{workspace}")
        print(f"当前状态：{services.repository.snapshot()['state'].phase}；等待后续 Verifier")
    finally:
        if backend is not None:
            await backend.close()
        database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true", help="使用流式模型调用")
    options = parser.parse_args()
    asyncio.run(main(stream=options.stream))
