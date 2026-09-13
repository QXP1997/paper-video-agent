import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from tests.support.loop_runtime import LoopRuntime
from tests.support.workspaces import stage
from qharness.exception import LoopExecutionError, LoopTransitionError
from qharness.loop.config import LoopConfig
from qharness.loop.models import StartStage, Wait
from qharness.loop.repository import LoopRepository
from qharness.persistence import OrmBase
from qharness.tools.base import ToolExecutionPolicy, ToolRuntimePolicy
from qharness.workspace import SqlAlchemyWorkspaceHistoryRepository


class RepositoryTests(unittest.TestCase):
    def runtime(self, **kwargs):
        runtime = LoopRuntime(**kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def test_migration_upgrades_existing_workspace_data_and_matches_metadata(self):
        runtime = self.runtime(initialize=False)
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "src/qharness/persistence/alembic"))
        with runtime.database.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_workspace_history")
        history = SqlAlchemyWorkspaceHistoryRepository(runtime.database.session_factory,
            tenant_id="tenant", workspace_id="workspace")
        history.bind_workspace_root(runtime.context.workspace.root)
        operation = history.begin_operation(run_id="old-run", tool_name="write_file", origin="agent",
                                            paths=("old.py",), base_commit_id=None)
        history.complete_operation(operation, "a" * 40)
        runtime.database.initialize()
        self.assertEqual(history.get_operation(operation).commit_id, "a" * 40)
        self.assertIn("loop_artifacts", inspect(runtime.database.engine).get_table_names())
        with runtime.database.engine.connect() as connection:
            self.assertEqual(connection.execute(text("select version_num from alembic_version")).scalar(), "0002_loop_ledger")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), OrmBase.metadata), [])

    def test_run_state_compare_and_swap_and_reconstruction_keeps_budget(self):
        runtime = self.runtime()
        repo = runtime.repository
        event = StartStage(run_id=repo.run_id, expected_version=runtime.state.version, plan=stage(), attempt_id="A1")
        state = repo.apply(event)
        with self.assertRaises(LoopTransitionError):
            repo.apply(event)
        repo.admit_tool("call", "check", {}, {"run_version": state.version}, runtime.policy)
        rebuilt = LoopRepository(runtime.database.session_factory, tenant_id="tenant", workspace_id="workspace", run_id=repo.run_id)
        rebuilt.create(runtime.state.contract, runtime.config, runtime.policy, state=runtime.state)
        self.assertEqual(rebuilt.snapshot()["state"], state)
        self.assertEqual(rebuilt.snapshot()["budget"]["tool_calls"], 1)
        with self.assertRaises(LoopExecutionError):
            rebuilt.create(runtime.state.contract, LoopConfig(max_model_attempts=999), runtime.policy)

    def test_atomic_logical_admission_and_one_dispatch_under_concurrency(self):
        runtime = self.runtime(policy=ToolExecutionPolicy(max_total_calls=1))
        repo = runtime.repository
        binding = {"run_version": runtime.state.version}
        def admit(_):
            repo.admit_tool("same", "check", {}, binding, runtime.policy)
            return repo.claim_tool("same")["status"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(admit, range(6)))
        self.assertEqual(results.count("execute"), 1)
        self.assertEqual(results.count("unknown"), 5)
        self.assertEqual(repo.snapshot()["budget"]["tool_calls"], 1)
        self.assertEqual(repo.snapshot()["budget"]["tool_executions"], 1)
        with self.assertRaises(LoopExecutionError):
            repo.admit_tool("another", "check", {}, binding, runtime.policy)

    def test_changed_arguments_and_stale_unexecuted_work_rejected(self):
        runtime = self.runtime()
        repo = runtime.repository
        binding = {"run_version": runtime.state.version}
        repo.admit_tool("call", "check", {}, binding, runtime.policy)
        with self.assertRaises(LoopExecutionError):
            repo.admit_tool("call", "check", {"different": True}, binding, runtime.policy)
        repo.apply(Wait(run_id=repo.run_id, expected_version=runtime.state.version, reason="新输入"))
        with self.assertRaises(LoopExecutionError):
            repo.claim_tool("call")

    def test_tenant_scope_is_applied_to_reads(self):
        runtime = self.runtime()
        other = LoopRepository(runtime.database.session_factory, tenant_id="other", workspace_id="workspace", run_id=runtime.state.run_id)
        with self.assertRaises(LoopExecutionError):
            other.snapshot()
        with self.assertRaises(LoopExecutionError):
            other.read_artifact("a" * 64)

    def test_model_budget_reservation_counts_network_attempts_and_unknown_usage(self):
        runtime = self.runtime(config=LoopConfig(max_model_attempts=2, max_total_tokens=25))
        repo = runtime.repository
        request = {"messages": []}
        binding = {"run_version": runtime.state.version}
        first = repo.begin_model("M1", "actor", request, binding, 10)
        repo.finish_model("M1", first["attempt"], error="网络失败", retryable=True)
        second = repo.begin_model("M1", "actor", request, binding, 10)
        self.assertEqual(second["attempt"], 2)
        repo.finish_model("M1", second["attempt"], error="网络失败", retryable=True)
        self.assertEqual(repo.snapshot()["budget"]["reserved_tokens"], 20)
        with self.assertRaises(LoopExecutionError):
            repo.begin_model("M2", "actor", request, binding, 10)

    def test_tool_execution_budget_is_separate_from_logical_admission(self):
        runtime = self.runtime(config=LoopConfig(max_tool_executions=1),
                               policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=3)))
        repo = runtime.repository
        for call_id in ("a", "b"):
            repo.admit_tool(call_id, "check", {}, {"run_version": runtime.state.version}, runtime.policy)
        repo.claim_tool("a")
        with self.assertRaises(LoopExecutionError):
            repo.claim_tool("b")
        self.assertEqual(repo.snapshot()["budget"]["tool_calls"], 2)
