"""基于真实 SQLite、工作区和工具执行器的离线装配。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from qharness.loop.config import LoopConfig
from qharness.loop.repository import LoopRepository
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.run import RunContext
from qharness.sandbox.base import SandboxBackend, SandboxExecutionResult, SandboxStatus
from qharness.tools.base import ToolExecutionPolicy
from qharness.tools.executor import ToolExecutor
from qharness.tools.registry import ToolRegistry
from qharness.workspace import WorkspaceContext
from tests.support.workspaces import pagination_run


class RecordingSandbox(SandboxBackend):
    def __init__(self):
        self.requests = []
        self.exit_code = 1
        self.stdout = "x" * 5000
        self.on_execute = None

    async def check_status(self):
        return SandboxStatus("scripted", True, "离线替身")

    async def execute(self, request):
        self.requests.append(request)
        if self.on_execute:
            self.on_execute(request)
        return SandboxExecutionResult(exit_code=self.exit_code, stdout=self.stdout, stderr="fixture failure",
            duration_seconds=0.01, run_id=request.run_id, operation_id=request.operation_id)


class LoopRuntime:
    def __init__(self, *, config=None, policy=None, state=None, initialize=True):
        self.temporary = TemporaryDirectory(prefix="qharness-ledger-")
        self.root = Path(self.temporary.name)
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.database = DatabaseManager(DatabaseConfig(url=f"sqlite:///{(self.root / 'app.sqlite3').as_posix()}"))
        if initialize:
            self.database.initialize()
        self.state = state or pagination_run()
        self.config = config or LoopConfig(retry_delay_seconds=0.0)
        self.policy = policy or ToolExecutionPolicy()
        self.repository = LoopRepository(self.database.session_factory, tenant_id="tenant", workspace_id="workspace",
                                         run_id=self.state.run_id)
        self.sandbox = RecordingSandbox()
        self.context = RunContext("tenant", "workspace", self.state.run_id, WorkspaceContext(workspace), self.sandbox)
        self.registry = ToolRegistry()
        self.executor = ToolExecutor(self.registry, policy=self.policy)
        if initialize:
            self.repository.create(self.state.contract, self.config, self.policy, state=self.state)

    def close(self):
        self.database.close()
        self.temporary.cleanup()
