import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.report import aggregate, distribution
from evals.run import ROOT, main, model_identity, parser, price, run_one, schedule
from evals.strategies import VARIANTS, configure
from evals.tasks import CATEGORIES, SPLITS, suite
from qharness.loop.config import LoopConfig
from qharness.loop.models import RunState, StagePlan, TodoPlan, TodoPlanPatch
from qharness.model.config import ModelBackendConfig
from qharness.sandbox.base import SandboxBackend, SandboxExecutionResult, SandboxStatus
from qharness.sandbox.config import load_sandbox_config
from qharness.tools.base import ToolExecutionPolicy, ToolRuntimePolicy
from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.task_flow import answer, handed_back, write


def fixes(task):
    if task.category in ("explicit", "investigation", "steering"):
        return {"pagination.py": "def paginate(items,page,size):\n    if page<=0 or size<=0: raise ValueError('invalid')\n    return items[(page-1)*size:page*size]\n"}
    if task.category == "environment":
        return {"settings.json": '{"data_file":"public_data.json"}'}
    return {f"step{i}.py": f"def transform(value):\n    return value + {i+1}\n" for i in range(len(task.requirements))}


class DatasetTests(unittest.TestCase):
    def test_splits_fingerprints_and_contracts(self):
        tasks = suite()
        self.assertEqual(len(tasks), 18)
        self.assertEqual(len({t.fingerprint for t in tasks}), 18)
        self.assertEqual(len({t.id for t in tasks}), 18)
        for split in SPLITS:
            self.assertEqual({t.category for t in tasks if t.split == split}, set(CATEGORIES))
        for t in tasks:
            t.todos().validate_contract(t.contract("test"))
            self.assertEqual(t.fingerprint, next(x.fingerprint for x in suite() if x.id == t.id))

    def test_originals_fail_fixes_pass_all_independent_oracles(self):
        # Only repository-authored fixtures run on host in this test; live model outputs always use SRT.
        for task in suite():
            with self.subTest(task=task.id), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for name, content in task.files.items():
                    (root/name).write_text(content, encoding="utf-8")
                def evaluate():
                    return subprocess.run([sys.executable,"-I","-B","-"], input=task.oracle(terminal=True,steered=task.category=="steering"),
                        cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=10)
                self.assertNotEqual(evaluate().returncode, 0)
                for name, content in fixes(task).items():
                    (root/name).write_text(content,encoding="utf-8")
                result=evaluate()
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn("QHARNESS_CHECK_OK",result.stdout)
                (root/"public_data.json").write_text("[]",encoding="utf-8")
                self.assertNotEqual(evaluate().returncode,0)

    def test_same_budget_all_variants_and_distinct_schedules(self):
        config=LoopConfig()
        ignored={"layered_feedback","dynamic_stage_planning","track_gap_progress"}
        for variant in VARIANTS:
            value=configure(config,variant)
            self.assertEqual({k:v for k,v in value.model_dump().items() if k not in ignored},
                             {k:v for k,v in config.model_dump().items() if k not in ignored})
        a=schedule(suite(),["base","all"],3,10)
        self.assertEqual(len(a),108)
        self.assertEqual(a,schedule(suite(),["base","all"],3,10))
        self.assertNotEqual(a,schedule(suite(),["base","all"],3,11))
        with self.assertRaises(ValueError): schedule(suite(),["all","all"],1,10)

    def test_terminal_has_inputs_not_in_online_check(self):
        task=suite()[0]
        self.assertNotEqual(task.oracle(),task.oracle(terminal=True))
        self.assertNotIn(task.terminal_assertions[0],task.catalog().context())


class ReportTests(unittest.TestCase):
    def test_environment_blocked_not_success_or_failure(self):
        report=aggregate([{"split":"dev","variant":"all","attempted":False}])[0]
        self.assertEqual(report["attempted"],0)
        self.assertIsNone(report["acceptance_rate"])
        self.assertIsNone(report["false_completion_rate"])
        self.assertIsNone(report["seconds"]["median"])

    def test_false_completion_separate_from_unknown_and_candidate(self):
        rows=[{"split":"dev","variant":"all","attempted":True,"phase":phase,"oracle_status":oracle,"seconds":1}
              for phase,oracle in (("completed","fail"),("completed","error"),("waiting","pass"),("verifying","fail"))]
        r=aggregate(rows)[0]
        self.assertEqual((r["accepted"],r["false_completion"],r["unverified_completion"]),(1,1,1))
        self.assertEqual(r["acceptance_rate"],.25)
        self.assertIsNone(r["route_quality"])

    def test_distributions_and_no_secret_in_model_manifest(self):
        self.assertEqual(distribution([1,2,3,4])["median"],2.5)
        cfg=ModelBackendConfig("fixture","https://host/secret-path","private-key","test")
        serialized=json.dumps(model_identity(cfg))
        self.assertNotIn("private-key",serialized)
        self.assertNotIn("secret-path",serialized)
        self.assertEqual(price("0"),0)
        for invalid in ("nan","inf","-1"):
            with self.assertRaises(Exception):price(invalid)
        rows=[{"task":task,"split":"dev","variant":"all","attempted":True,"phase":"completed","seconds":1,"oracle_status":status}
              for task,statuses in (("a",("pass","pass","fail")),("b",("pass","pass","pass"))) for status in statuses]
        result=aggregate(rows)[0]
        self.assertAlmostEqual(result["pass_k"][0]["pass_all_k"],2/3)
        self.assertEqual(result["pass_k"][1]["pass_all_k"],.5)
        rows[0]["budget_within_limits"]=False
        self.assertEqual(aggregate(rows)[0]["accepted"],4)


class FixtureSandbox(SandboxBackend):
    """Execute only the fixed oracle source with scripted, repository-authored code in evaluator tests."""
    def __init__(self, workspace): self.workspace=workspace
    async def check_status(self):return SandboxStatus("fixture",True,"offline test",version="1")
    async def execute(self,request):
        if request.stdin:
            source=request.stdin
        else:
            import base64,re
            encoded=re.search(r"b64decode\('([^']+)'\)",request.command).group(1)
            source=base64.b64decode(encoded).decode("utf-8")
        process=await asyncio.create_subprocess_exec(sys.executable,"-I","-B","-",cwd=self.workspace.root,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await process.communicate(source.encode("utf-8"))
        return SandboxExecutionResult(process.returncode,stdout.decode("utf-8"),stderr.decode("utf-8"),.01,
                                      run_id=request.run_id,operation_id=request.operation_id)


def script_for(task, variant):
    def plan(request):
        data=json.loads(request.messages[1].content)
        state=RunState.model_validate(data["run_state"])
        todo=data["current_todo"]["todo"]
        return answer(StagePlan(stage_id=f"S{len(state.attempts)+1}",todo_id=todo["id"],todo_version=todo["version"],
            kind="implement",objective=todo["objective"],addresses=tuple(todo["acceptance_refs"]),
            expected_results=tuple(todo["done_when"]),stop_when=("候选实现已写入",),replan_when=("前提改变",)))
    sequence=[]
    if variant not in ("react","fixed_todo"):
        sequence.extend([answer(task.todos()),plan])
    sequence.extend([write(name,content) for name,content in fixes(task).items()])
    if task.category=="steering" and variant not in ("react","fixed_todo"):
        sequence.append(plan)
    sequence.append(handed_back())
    for todo in task.todos().todos[1:] if variant!="react" else ():
        if variant!="fixed_todo":sequence.append(plan)
        sequence.append(handed_back())
    return ScriptedModelBackend(sequence)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate_script(self, task, backend, variant="base"):
        with tempfile.TemporaryDirectory() as temp:
            with patch("evals.run.OpenAICompatibleBackend",return_value=backend),patch("qharness.run.factory.create_sandbox_backend",side_effect=lambda config,workspace,*args:FixtureSandbox(workspace)):
                return await run_one(task,variant,0,Path(temp),LoopConfig(context_compaction=True),
                    ModelBackendConfig("fixture","https://fixture","secret","fixture"),
                    load_sandbox_config(ROOT/"config/sandbox.example.toml"),
                    ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100)),(None,None))

    async def test_uncovered_todo_and_stage_are_corrected_before_actions(self):
        task=suite()[0]
        good=list(script_for(task,"base")._script)
        bad=task.todos().model_dump()
        bad["todos"][0]["done_when"] += ("只在 Task 检查的约束",)
        def bad_stage(request):
            response=good[1](request)
            plan=StagePlan.model_validate_json(response.message.content)
            return answer(plan.model_copy(update={"expected_results":(*plan.expected_results,"未覆盖的结果")}))
        backend=ScriptedModelBackend([answer(TodoPlan.model_validate(bad)),good[0],bad_stage,*good[1:]])
        result=await self.evaluate_script(task,backend)
        self.assertEqual(result["oracle_status"],"pass",result)
        self.assertEqual(result["budget"]["model_attempts"],6)
        self.assertEqual(result["budget"]["tool_executions"],4)
        self.assertIn("TODO",json.loads(backend.requests[1].messages[1].content)["observations"][-1])
        self.assertIn("STAGE",json.loads(backend.requests[3].messages[1].content)["observations"][-1])

    async def test_missing_stage_coverage_exhausts_bounded_corrections_without_actor(self):
        task=suite()[0]
        good=list(script_for(task,"base")._script)
        def invalid(request):
            plan=StagePlan.model_validate_json(good[1](request).message.content)
            return answer(plan.model_copy(update={"expected_results":("模型自报完成",)}))
        result=await self.evaluate_script(task,ScriptedModelBackend([good[0],invalid,invalid,invalid]))
        self.assertEqual(result["phase"],"waiting",result)
        self.assertEqual(result["budget"]["tool_executions"],0)
        self.assertNotIn("actor",result["role_attempts"])
        self.assertEqual(result["oracle_status"],"fail")

    async def test_full_replan_actually_calls_global_planner_after_failure(self):
        task=suite()[0]
        good=list(script_for(task,"full_replan")._script)
        def revised(request):
            state=RunState.model_validate(json.loads(request.messages[1].content)["run_state"])
            return answer(TodoPlanPatch(base_version=state.todo_plan.version,
                plan=state.todo_plan.model_copy(update={"version":state.todo_plan.version+1,
                    "todos":(state.todo_plan.todos[0].model_copy(update={"id":"T-repair","objective":"依据失败断言重新组织分页修复"}),)}),
                reason="重新分解失败任务，保留原验收义务",evidence_refs=state.pending_decision.evidence_refs))
        backend=ScriptedModelBackend([good[0],good[1],write("pagination.py","def paginate(items,page,size): return []\n"),good[3],revised,*good[1:]])
        result=await self.evaluate_script(task,backend,"full_replan")
        self.assertEqual(result["oracle_status"],"pass",(result,json.loads(backend.requests[-1].messages[1].content)["observations"][-1]))
        self.assertEqual(result["routes"].get("replan_todo"),1)
        self.assertEqual(result["role_attempts"]["todo_planner"],2)
        self.assertEqual(result["budget"]["model_attempts"],8)

    async def test_independent_scorer_rejects_early_exit_false_completion(self):
        task=suite()[0]
        good=list(script_for(task,"base")._script)
        good[2]=write("pagination.py","raise SystemExit(0)\n")
        result=await self.evaluate_script(task,ScriptedModelBackend(good))
        self.assertEqual(result["phase"],"completed",result)
        self.assertEqual(result["oracle_status"],"fail")
        self.assertEqual(aggregate([result])[0]["false_completion"],1)

    async def test_steering_and_long_scenarios_exercise_persistent_inputs(self):
        for category in ("steering","long_running"):
            task=next(t for t in suite() if t.id=="dev-"+category)
            with self.subTest(category=category),tempfile.TemporaryDirectory() as temp:
                with patch("evals.run.OpenAICompatibleBackend",return_value=script_for(task,"all")),patch("qharness.run.factory.create_sandbox_backend",side_effect=lambda config,workspace,*args:FixtureSandbox(workspace)):
                    result=await run_one(task,"all",0,Path(temp),LoopConfig(context_compaction=True),
                        ModelBackendConfig("fixture","https://fixture","secret","fixture"),
                        load_sandbox_config(ROOT/"config/sandbox.example.toml"),
                        ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100)),(None,None))
                self.assertEqual(result["oracle_status"],"pass",result)
                self.assertTrue(result["scenario_triggered"])
                self.assertTrue(result["independent_check"]["scenario_exercised"])
                self.assertIsNone(result["model_cost"])

    async def test_real_runner_assembly_scoring_and_budget_all_primary_arms(self):
        task=next(t for t in suite() if t.id=="dev-explicit")
        for variant in ("react","fixed_todo","full_replan","base","all"):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as temp:
                backend=script_for(task,variant)
                with patch("evals.run.OpenAICompatibleBackend",return_value=backend),patch("qharness.run.factory.create_sandbox_backend",side_effect=lambda config,workspace,*args:FixtureSandbox(workspace)):
                    result=await run_one(task,variant,0,Path(temp),LoopConfig(),
                        ModelBackendConfig("fixture","https://fixture","secret","fixture"),
                        load_sandbox_config(ROOT/"config/sandbox.example.toml"),
                        ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100)),(1.0,2.0))
                self.assertEqual(result["oracle_status"],"pass",result)
                self.assertEqual(result["phase"],"verifying" if variant=="react" else "completed",result)
                self.assertEqual(result["budget"]["model_attempts"],2 if variant in ("react","fixed_todo") else 4)
                self.assertIsNotNone(result["model_cost"])
                self.assertFalse(result["unknown_calls"])

    async def test_unavailable_sandbox_never_calls_model_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            args=parser().parse_args(["--output",temp,"--categories","explicit","--repeats","1",
                "--model-config",str(ROOT/"config/model.example.toml"),"--tool-config",str(ROOT/"config/tool.example.toml"),
                "--sandbox-config",str(ROOT/"config/sandbox.example.toml")])
            class Unavailable:
                async def check_status(self):return SandboxStatus("srt",False,"fixture unavailable")
            with patch("evals.run.create_sandbox_backend",return_value=Unavailable()),patch("evals.run.OpenAICompatibleBackend") as model:
                code=await main(args)
                model.assert_not_called()
            document=json.loads(next(Path(temp).rglob("report.json")).read_text(encoding="utf-8"))
            self.assertEqual(code,2)
            self.assertEqual(document["status"],"blocked_environment")
            self.assertTrue(all(not r["attempted"] for r in document["results"]))


if __name__=="__main__":unittest.main()
