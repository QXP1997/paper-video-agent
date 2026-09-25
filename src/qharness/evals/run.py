"""python -m evals.run --split dev --variants base all --repeats 3

仅通过原 ModelBackend / RunService / SRT 执行。--plan 不调用模型或执行沙箱命令。
"""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import platform
from importlib.metadata import version
from pathlib import Path
import random
import time
import traceback
import uuid

from qharness.backends import OpenAICompatibleBackend
from qharness.loop.config import load_loop_config
from qharness.loop.repository import digest
from qharness.loop.transitions import create_run
from qharness.model.config import load_model_config
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.run import RunService, create_loop_services, create_run_context
from qharness.sandbox.base import SandboxExecutionRequest
from qharness.sandbox.config import load_sandbox_config
from qharness.sandbox.factory import create_sandbox_backend
from qharness.tools import (BuiltinToolProvider, FileMutationToolProvider, SandboxToolProvider,
    ToolExecutor, ToolRegistry, load_tool_policy, load_tool_providers)
from qharness.tools.hooks import ToolExecutionHook
from qharness.workspace import WorkspaceContext, WorkspaceHistoryConfig

from .report import aggregate, markdown
from .strategies import DESCRIPTIONS, VARIANTS, configure, install
from .tasks import CATEGORIES, SPLITS, STEERING, SUITE_VERSION, command_for, suite


ROOT = Path(__file__).resolve().parents[1]


def save(path, document):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def schedule(tasks, variants, repeats, seed):
    if repeats < 1 or len(variants) != len(set(variants)) or not variants:
        raise ValueError("重复次数须大于零，策略不可为空或重复")
    jobs = [(task, variant, repeat) for task in tasks for repeat in range(repeats) for variant in variants]
    random.Random(seed).shuffle(jobs)
    return jobs


def model_identity(config):
    # API key is excluded even from the manifest digest; endpoint identity is stored as a digest only.
    return {"provider": config.provider, "model": config.model, "endpoint_hash": digest(config.base_url),
            "temperature": config.temperature, "thinking_mode": config.thinking_mode,
            "reasoning_effort": config.reasoning_effort, "extra_body_hash": digest(config.extra_body),
            "timeout_seconds": config.timeout_seconds, "max_tokens": config.max_tokens}


def sandbox_identity(config):
    return json.loads(json.dumps(asdict(config), default=str))


def error_identity(error):
    frame = traceback.extract_tb(error.__traceback__)[-1]
    return {"type": type(error).__name__, "file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}


class ScenarioHook(ToolExecutionHook):
    def __init__(self, category):
        self.category, self.service, self.triggered = category, None, False

    async def after_execute(self, request, tool, result):
        if self.triggered or self.category not in ("steering", "long_running"):
            return
        if self.category == "long_running" and tool.name not in ("write_file", "edit_file", "run_command"):
            return
        self.triggered = True
        await self.service.submit("scenario-pause", "pause")


def metrics(repository, state, rates):
    snapshot = repository.snapshot()
    roles, routes = Counter(), Counter()
    models_seen = set()
    prompt = completion = 0
    usage_complete = True
    for call in repository.recovery_inventory():
        if call["kind"] != "model":
            continue
        for attempt in repository.model_trace(call["call_id"]):
            roles[attempt["role"]] += 1
            usage = (attempt["response"] or {}).get("usage")
            if (attempt["response"] or {}).get("model"):
                models_seen.add(attempt["response"]["model"])
            if not attempt["usage_known"] or not usage:
                usage_complete = False
            else:
                prompt += usage["prompt_tokens"]
                completion += usage["completion_tokens"]
    attempts = state.attempts if state else ()
    for attempt in attempts:
        if attempt.decision:
            routes[attempt.decision.route.value] += 1
    reports = [a.verdicts[-1].progress_report for a in attempts if a.plan.kind == "investigate" and a.verdicts and a.verdicts[-1].progress_report]
    investigations = sum(a.plan.kind == "investigate" for a in attempts)
    return {"budget": snapshot["budget"], "role_attempts": dict(roles), "routes": dict(routes),
        "repair_attempts": routes["repair"], "task_rejections": sum(v.status == "fail" for v in state.task_verdicts) if state else 0,
        "investigation_attempts": investigations, "models_seen": sorted(models_seen),
        "progress_reports": len(reports), "investigations_without_novelty": sum(r.novel_refs == () for r in reports) if len(reports)==investigations else None,
        "model_usage_complete": usage_complete, "prompt_tokens": prompt, "completion_tokens": completion,
        "model_cost": (prompt*rates[0]+completion*rates[1])/1_000_000 if usage_complete and all(r is not None for r in rates) else None,
        "unknown_calls": repository.unresolved_calls(), "pending_inputs": len(repository.inputs())}


async def probe_model(directory, config, model_config, sandbox_config, tool_policy):
    """独立的模型规划连通性检查，不执行 Actor，不计入任务完成率。"""
    task = next(t for t in suite() if t.id == "dev-explicit")
    area = directory / "model-probe"
    workspace = area / "workspace"
    workspace.mkdir(parents=True)
    for name, text in task.files.items():
        (workspace/name).write_text(text, encoding="utf-8")
    database = DatabaseManager(DatabaseConfig(url="sqlite:///"+(area/"probe.sqlite3").as_posix()))
    backend = OpenAICompatibleBackend(model_config)
    result = {"status": "not_run", "scope": "Todo / Stage 规划协议；无 Actor 或沙箱命令，不是任务验收"}
    services = None
    try:
        context = create_run_context(tenant_id="eval-probe",workspace_id="probe",run_id="probe",
            workspace_root=workspace,sandbox_config=sandbox_config)
        context.metadata["verification_environment"] = "planning-probe-no-execution"
        services = create_loop_services(context,backend=backend,executor=ToolExecutor(ToolRegistry(),policy=tool_policy),
            database_manager=database,config=config,contract=task.contract("probe"))
        notes = (task.catalog().context(), "done_when 和 expected_results 必须使用目录 targets 中的原文，不得添加目录不支持的验收标签。")
        state = await services.executor.planner.initialize(observations=notes,checks=task.catalog())
        state = await services.executor.planner.stage(state,observations=notes,checks=task.catalog())
        selected = task.catalog().stage_checks(state)
        expected = set(state.attempts[-1].plan.expected_results)
        coverage = {t for s in selected if s.scope == "stage" for t in s.targets}
        todo = next(t.todo for t in state.todos if t.todo.id == state.attempts[-1].plan.todo_id)
        todo_coverage = {t for s in selected if s.scope == "todo" for t in s.targets}
        grounded = expected <= coverage and set((*todo.acceptance_refs,*todo.done_when)) <= todo_coverage
        result["status"] = "pass" if grounded else "ungrounded_plan"
        result["state"] = state.model_dump(mode="json")
    except Exception as error:
        result["status"], result["error"] = "error", error_identity(error)
    finally:
        if services:
            result.update(metrics(services.repository,services.repository.snapshot()["state"],(None,None)))
        await backend.close()
        database.close()
        save(area/"result.json",result)
    return result


async def run_one(task, variant, repeat, directory, config, model_config, sandbox_config, tool_policy, rates):
    run_id = f"{task.id}-{variant}-{repeat}-{uuid.uuid4().hex[:8]}"
    area = directory / run_id
    workspace = area / "workspace"
    workspace.mkdir(parents=True)
    for name, content in task.files.items():
        (workspace / name).write_text(content, encoding="utf-8")
    database = DatabaseManager(DatabaseConfig(url="sqlite:///" + (area / "run.sqlite3").as_posix()))
    history = WorkspaceHistoryConfig(storage_root=area / "history")
    backend = OpenAICompatibleBackend(model_config)
    record = {"task": task.id, "split": task.split, "category": task.category, "task_hash": task.fingerprint,
              "variant": variant, "repeat": repeat, "run_id": run_id, "attempted": False,
              "phase": None, "oracle_status": "not_run", "seconds": 0, "workspace": str(workspace)}
    service = None
    start = time.perf_counter()
    try:
        context = create_run_context(tenant_id="local-eval", workspace_id=run_id, run_id=run_id,
            workspace_root=workspace, sandbox_config=sandbox_config, database_manager=database, history_config=history)
        status = await context.sandbox.check_status()
        if not status.available:
            record["error"] = "sandbox_unavailable"
            return record
        context.metadata["verification_environment"] = digest([SUITE_VERSION, task.fingerprint, asdict(status), sandbox_identity(sandbox_config)])
        registry = ToolRegistry()
        await load_tool_providers(registry, [BuiltinToolProvider(context.workspace),
            FileMutationToolProvider(context.mutation_service), SandboxToolProvider(context)])
        hook = ScenarioHook(task.category)
        executor = ToolExecutor(registry, policy=tool_policy, hooks=[hook])
        contract = task.contract(run_id)
        initial = create_run(run_id, contract, task.todos(single=variant == "react")) if variant in ("react", "fixed_todo") else None
        services = create_loop_services(context, backend=backend, executor=executor, database_manager=database,
            config=configure(config, variant), contract=contract, state=initial)
        services = install(services, task, variant)
        service = RunService(services)
        hook.service = service
        checks = task.catalog()
        notes = ("运行环境为 Windows SRT，python 是托管解释器。所有 Todo.done_when 和 Stage.expected_results 必须逐字复制检查目录 targets，不能使用自拟同义句；Task 专属约束只留给最终 Task 验证。先读取现有文件。",)
        record["attempted"] = True
        try:
            state = await service.run(checks, observations=notes)
            if hook.triggered:
                if task.category == "steering":
                    await service.submit("scenario-steer", "steer", {"constraints": [STEERING]})
                    checks = task.catalog(steered=True)
                await service.submit("scenario-resume", "resume")
                # Rebuild the lifecycle controller; persistent identity/budget remain the same.
                service = RunService(services)
                hook.service = service
                state = await service.run(checks, observations=notes)
            record["phase"] = state.phase.value if state else None
        except Exception as error:
            # Keep raw model/provider text in the original protected ledger, not public reports.
            record["error"] = error_identity(error)
        state = services.repository.snapshot()["state"]
        record["phase"] = state.phase.value if state else None
        record["candidate"] = bool(state and state.attempts and state.attempts[-1].outcome and state.attempts[-1].outcome.status == "candidate")
        record["scenario_triggered"] = hook.triggered
        record["scenario_inputs"] = [{"kind": i["kind"], "status": i["status"]} for i in services.repository.inputs(None)]
        record["human_interventions"] = 0  # No automatic human approval or external repair is fabricated.
        record.update(metrics(services.repository, state, rates))
        budget = record["budget"]
        record["budget_within_limits"] = (budget["model_attempts"] <= config.max_model_attempts and
            budget["tool_executions"] <= config.max_tool_executions and
            (config.max_total_tokens is None or budget["used_tokens"]+budget["reserved_tokens"] <= config.max_total_tokens))
        save(area / "snapshot.json", {"state": state.model_dump(mode="json") if state else None, "budget": record["budget"]})
        if record["unknown_calls"]:
            record["oracle_status"] = "blocked_unknown_effect"
        else:
            # Independent scorer runs only after service quiescence and under the same workspace ownership.
            service.ownership.acquire()
            uncertain = True
            try:
                marker = "QHARNESS_FINAL_" + uuid.uuid4().hex
                steered = bool(state and STEERING in state.contract.constraints)
                source = task.oracle(terminal=True, steered=steered, marker=marker)
                # SRT 的 Windows stdin 转发不是所有版本都保持；把评分源码编码进固定 argv，
                # 与目录中的在线检查使用同一可审计命令表示，避免空 stdin 造成假失败。
                result = await context.sandbox.execute(SandboxExecutionRequest(command=command_for(source),
                    run_id=run_id, operation_id="independent-final", timeout_seconds=60))
                protected = all((workspace / p).is_file() and (workspace / p).read_text(encoding="utf-8") == task.files[p] for p in task.protected)
                applied = {i["kind"] for i in record["scenario_inputs"] if i["status"] == "applied"}
                valid_scenario = (task.category not in ("steering", "long_running") or
                    {"pause", "resume"} <= applied and (task.category != "steering" or "steer" in applied and steered))
                passed = result.succeeded and result.stdout.splitlines().count(marker) == 1 and protected and valid_scenario
                record["oracle_status"] = "pass" if passed else ("error" if result.timed_out or result.cancelled or result.exit_code is None or result.stdout_truncated or result.stderr_truncated else "fail")
                record["independent_check"] = {**asdict(result), "protected_files": protected, "scenario_exercised": valid_scenario,
                    "source_hash": digest(source), "accounting": "固定独立评分，不进入 Agent 预算；此耗时计入总耗时。"}
                save(area/"independent-check.json",record["independent_check"])
                uncertain = result.exit_code is None or result.timed_out or result.cancelled
            finally:
                service.ownership.release(uncertain=uncertain)
    except Exception as error:
        record["error"] = error_identity(error)
    finally:
        record["seconds"] = time.perf_counter()-start
        await backend.close()
        database.close()
        save(area / "result.json", record)
    return record


async def main(args):
    tasks = [t for t in suite() if t.split == args.split and (not args.categories or t.category in args.categories)]
    jobs = schedule(tasks, args.variants, args.repeats, args.seed)
    config = load_loop_config(ROOT / "config/loop.example.toml")
    model_config = load_model_config(args.model_config)
    sandbox_config = load_sandbox_config(args.sandbox_config)
    tool_policy = load_tool_policy(args.tool_config)
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = args.output.resolve() / batch_id
    output.mkdir(parents=True)
    manifest = {"suite_version": SUITE_VERSION, "tasks": {t.id: t.fingerprint for t in tasks},
        "host": {"platform": platform.platform(), "harness_python": platform.python_version(),
                 "dependencies": {name: version(name) for name in ("pydantic","SQLAlchemy","openai","dulwich","alembic")}},
        "model": model_identity(model_config), "loop": config.model_dump(mode="json"),
        "tool_policy": asdict(tool_policy), "sandbox_hash": digest(sandbox_identity(sandbox_config)),
        "variants": {v: DESCRIPTIONS.get(v, "三项策略开关组合："+v) for v in args.variants},
        "repeats": args.repeats, "seed": args.seed, "rates_per_million": [args.input_price, args.output_price],
        "code_hash": digest({str(p.relative_to(ROOT)): digest(p.read_bytes().hex()) for folder in (ROOT/"src/qharness", ROOT/"evals") for p in sorted(folder.rglob("*.py"))}),
        "schedule": [{"task": t.id, "variant": v, "repeat": r} for t,v,r in jobs]}
    document = {"batch_id": batch_id, "manifest": manifest, "manifest_hash": digest(manifest), "status": "planned", "results": []}
    save(output / "report.json", document)
    print("评测目录：", output, flush=True)
    if args.plan:
        print(f"已固定 {len(jobs)} 次运行；未调用模型或执行沙箱命令。", flush=True)
    else:
        sandbox = create_sandbox_backend(sandbox_config, WorkspaceContext(output))
        status = await sandbox.check_status()
        document["preflight"] = {"available": status.available, "backend": status.backend, "version": status.version, "message": status.message}
        if args.probe_model:
            print("检查真实模型的 Todo / Stage 输出协议（不执行工具）", flush=True)
            document["model_probe"] = await probe_model(output,config,model_config,sandbox_config,tool_policy)
            print("模型规划检查：",document["model_probe"]["status"],flush=True)
        if not status.available:
            document["status"] = "blocked_environment"
            document["results"] = [{"task": t.id, "split": t.split, "category": t.category, "variant": v, "repeat": r,
                                    "attempted": False, "error": "sandbox_unavailable"} for t,v,r in jobs]
        else:
            document["status"] = "running"
            save(output / "report.json", document)
            for task, variant, repeat in jobs:
                print(f"运行 {task.id} / {variant} / {repeat+1}", flush=True)
                record = await run_one(task, variant, repeat, output, config, model_config, sandbox_config, tool_policy,
                                       (args.input_price, args.output_price))
                document["results"].append(record)
                save(output / "report.json", document)
                print(f"状态 {record['phase']}，独立验收 {record['oracle_status']}", flush=True)
            document["status"] = "finished"
    document["summary"] = aggregate(document["results"])
    save(output / "report.json", document)
    (output / "report.md").write_text(markdown(document), encoding="utf-8")
    return 2 if document["status"] == "blocked_environment" else (1 if any(r.get("oracle_status") != "pass" for r in document["results"]) else 0)


def price(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("价格必须是非负有限数")
    return parsed


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--split", choices=SPLITS, default="dev")
    p.add_argument("--categories", nargs="+", choices=CATEGORIES)
    p.add_argument("--variants", nargs="+", choices=VARIANTS, default=["base", "all"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--seed", type=int, default=1729)
    p.add_argument("--plan", action="store_true")
    p.add_argument("--probe-model", action="store_true", help="额外检查真实模型规划协议；用量单列，不计入任务对照")
    p.add_argument("--model-config", type=Path, default=ROOT/"config/model.toml")
    p.add_argument("--sandbox-config", type=Path, default=ROOT/"config/sandbox.toml")
    p.add_argument("--tool-config", type=Path, default=ROOT/"config/tool.toml")
    p.add_argument("--output", type=Path, default=ROOT/".qharness/evals")
    p.add_argument("--input-price", type=price)
    p.add_argument("--output-price", type=price)
    return p


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parser().parse_args())))
