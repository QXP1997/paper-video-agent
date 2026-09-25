"""离线演示三个完成边界。验证结果为显式脚本输入，不是实际模型推理。"""

from _common import SOURCE_ROOT  # 沿用已有示例的源码路径初始化。

from qharness.loop import (
    ApplyFeedback, Criterion, EvidenceLink, FeedbackDecision, ProgressDelta,
    Question, QuestionFinding, RecordStageVerdict, RecordTaskVerdict, RunState,
    StageOutcome, StagePlan, StageVerdict, StartStage, SubmitOutcome,
    TaskContract, TaskVerdict, Todo, TodoPlan, create_run, reduce,
)


def emit(state, event_type, **payload):
    return reduce(state, event_type(run_id=state.run_id, expected_version=state.version, **payload))


def run_stage(state, stage_id, kind, *, todo_pass=False):
    state = emit(state, StartStage, attempt_id=f"attempt-{stage_id}", plan=StagePlan(
        stage_id=stage_id, todo_id="T1", todo_version=1, kind=kind,
        objective="定位分页问题" if kind == "investigate" else "修复并检查全部边界",
        addresses=("Q1",) if kind == "investigate" else ("C1", "C2"),
        expected_results=("可复现的定位结论",) if kind == "investigate" else ("两项验收全部通过",),
        stop_when=("已得到预期结果",), replan_when=("当前前提被观察推翻",),
    ))
    identity = state.attempts[-1].identity.model_dump()
    state = emit(state, SubmitOutcome, outcome=StageOutcome(
        **identity, status="candidate", summary="候选结果已就绪", observation_refs=("E-script",),
    ))
    progress = ProgressDelta(satisfied_criteria=tuple(
        EvidenceLink(ref=ref, evidence_refs=(f"E-script-{ref}",)) for ref in ("C1", "C2")
    )) if todo_pass else ProgressDelta(resolved_questions=(
        QuestionFinding(ref="Q1", finding="页号从 1 开始，偏移量多加了一页", evidence_refs=("E-script-repro",)),
    ))
    state = emit(state, RecordStageVerdict, verdict=StageVerdict(
        **identity, stage_status="pass", todo_status="pass" if todo_pass else "fail",
        expected_vs_observed="阶段预期结果已得到", progress=progress, evidence_refs=("E-script",),
        remaining_gaps=() if todo_pass else ("C1", "C2"),
    ))
    return emit(state, ApplyFeedback, decision=FeedbackDecision(route="advance", reason="阶段已通过"))


def main():
    state = create_run("run-demo", TaskContract(
        task_id="pagination", objective="修复分页边界并检查输入",
        criteria=(Criterion(id="C1", description="第一页及尾页正确"),
                  Criterion(id="C2", description="非正页号与页大小被拒绝")),
    ), TodoPlan(todos=(Todo(
        id="T1", objective="修复分页", acceptance_refs=("C1", "C2"), done_when=("分页回归通过",),
    ),)), questions=(Question(id="Q1", description="偏移量是否算错？", acceptance_refs=("C1",)),))

    state = run_stage(state, "S1", "investigate")
    print(f"调查阶段 PASS -> Todo={state.todos[0].status}, Run={state.phase}")
    state = run_stage(state, "S2", "implement", todo_pass=True)
    print(f"Todo PASS     -> Todo={state.todos[0].status}, Run={state.phase}")
    state = emit(state, RecordTaskVerdict, verdict=TaskVerdict(
        contract_version=state.contract.version, status="pass", summary="最终任务验收通过",
        criterion_evidence=tuple(EvidenceLink(ref=ref, evidence_refs=("E-script-final",))
                                 for ref in ("C1", "C2")),
    ))
    print(f"Task PASS     -> Run={state.phase}, attempts={len(state.attempts)}")
    assert RunState.model_validate_json(state.model_dump_json()) == state


if __name__ == "__main__":
    main()
