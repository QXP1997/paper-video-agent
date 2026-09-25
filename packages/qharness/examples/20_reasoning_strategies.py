"""三项推理策略的离线行为演示；--ablation 检查八种独立配置组合。

复用既有模型/沙箱 Fixture，文件、检查凭据、状态和预算真实落盘。
这些可控轨迹验证机制，不代表真实模型成功率或研究收益。
"""

import argparse
import asyncio
import itertools
import sys

from _common import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from qharness.loop import RunState
from qharness.loop.config import LoopConfig
from qharness.verification import CheckCatalog, CheckSpec, QuestionConclusion
from tests.support.task_flow import flow_runtime, guided_plan, handed_back, reasoning_catalog, write
from tests.support.workspaces import pagination_run


async def demonstration():
    specs = reasoning_catalog().specs
    original = next(s for s in specs if s.id == "investigate")
    original = CheckSpec.model_validate({**original.model_dump(), "conclusions": (
        QuestionConclusion(question_id="Q1", kind="eliminated", finding="排除输入校验分支", fact_id="not-input"),)})
    alternative = CheckSpec.model_validate({**original.model_dump(), "id": "alternative",
        "inputs": ("pagination.py", "validation.py"), "conclusions": (
            QuestionConclusion(question_id="Q1", finding="已定位分页偏移计算", fact_id="offset-reproduced"),)})
    checks = CheckCatalog(tuple(s for s in specs if s.id != "investigate") + (original, alternative))
    script = [item for _ in range(3) for item in (guided_plan(source="investigate"), handed_back())]
    script += [guided_plan(source="alternative"), handed_back(), guided_plan(), handed_back(),
               write("pagination.py", "fixed"), handed_back(), guided_plan(), write("validation.py", "validated"), handed_back()]
    config = LoopConfig(layered_feedback=True, dynamic_stage_planning=True, track_gap_progress=True)
    runtime = flow_runtime(script, config=config)
    try:
        state = await runtime.services.executor.run(checks)
        for attempt in state.attempts:
            report = attempt.verdicts[-1].progress_report
            decision = attempt.decision
            print(f"{attempt.plan.stage_id} v{attempt.plan.plan_version} {attempt.plan.kind}: "
                  f"novel={report.novel_refs}, stalled={report.stalled_investigations} → {decision.route}")
            print("  preserve:", decision.preserve)
            print("  invalidate:", decision.invalidate, "focus:", decision.focus)
        print("最终状态:", state.phase, "Task:", [v.status.value for v in state.task_verdicts])
        runtime.backend.assert_exhausted()
        assert state.phase == "completed"
    finally:
        runtime.close()


async def ablation():
    print("layered dynamic gaps | phase | attempts model_attempts tool_calls reports")
    for layered, dynamic, gaps in itertools.product((False, True), repeat=3):
        initial = pagination_run()
        initial = RunState.model_validate({**initial.model_dump(), "questions": ()})
        config = LoopConfig(layered_feedback=layered, dynamic_stage_planning=dynamic, track_gap_progress=gaps)
        runtime = flow_runtime([guided_plan(), handed_back(), write("pagination.py", "fixed"), handed_back(),
            guided_plan(), write("validation.py", "validated"), handed_back()], config=config, state=initial)
        try:
            state = await runtime.services.executor.run(reasoning_catalog())
            budget = runtime.repository.snapshot()["budget"]
            reports = sum(v.progress_report is not None for a in state.attempts for v in a.verdicts)
            print(f"{int(layered):7} {int(dynamic):7} {int(gaps):4} | {state.phase} | "
                  f"{len(state.attempts):8} {budget['model_attempts']:14} {budget['tool_calls']:10} {reports:7}")
            runtime.backend.assert_exhausted()
            assert state.phase == "completed" and state.task_verdicts[-1].status == "pass"
        finally:
            runtime.close()
    print("相同可控局部修复任务的配置对照；不以此推断策略在真实任务上的优劣。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ablation", action="store_true")
    asyncio.run(ablation() if parser.parse_args().ablation else demonstration())
