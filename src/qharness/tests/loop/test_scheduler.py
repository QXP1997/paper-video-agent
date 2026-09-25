import asyncio
import unittest

from tests.loop.test_transitions import begin
from tests.support.loop_runtime import LoopRuntime
from qharness.exception import ToolRegistrationError
from qharness.loop.scheduler import ActionScheduler
from qharness.loop.tool_service import ToolService
from qharness.model.models import FunctionCall, ToolCall
from qharness.tools.base import Tool, ToolEffect, ToolParameters


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_reads_overlap_then_write_barrier_then_read(self):
        runtime = LoopRuntime(state=begin())
        self.addCleanup(runtime.close)
        active, peak = 0, 0
        timeline = []
        both_started = asyncio.Event()
        async def read():
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
            timeline.append("read")
            active -= 1
            return {"ok": True}
        async def write():
            self.assertEqual(active, 0)
            self.assertEqual(timeline, ["read", "read"])
            timeline.append("write")
            return {"ok": True}
        runtime.registry.register(Tool("read", "独立读取", ToolParameters, read, effect=ToolEffect.READ_ONLY, parallel_safe=True))
        runtime.registry.register(Tool("write", "写屏障", ToolParameters, write, effect=ToolEffect.WORKSPACE_WRITE))
        scheduler = ActionScheduler(ToolService(runtime.executor, runtime.context, runtime.repository), max_parallel_reads=2)
        results = await scheduler.execute([ToolCall(str(i), FunctionCall(name, "{}")) for i, name in enumerate(
            ("read", "read", "write", "read"))], model_call_id="M", expected_version=runtime.state.version)
        self.assertEqual(peak, 2)
        self.assertTrue(all(r.result.success for r in results))
        self.assertEqual(timeline, ["read", "read", "write", "read"])

    async def test_unknown_tools_are_sequential_and_write_cannot_declare_parallel_safe(self):
        runtime = LoopRuntime(state=begin())
        self.addCleanup(runtime.close)
        active = 0
        async def handler():
            nonlocal active
            self.assertEqual(active, 0)
            active += 1
            await asyncio.sleep(0)
            active -= 1
            return {}
        runtime.registry.register(Tool("unknown", "默认未知效果", ToolParameters, handler))
        with self.assertRaises(ToolRegistrationError):
            runtime.registry.register(Tool("bad", "错误声明", ToolParameters, handler,
                                          effect=ToolEffect.WORKSPACE_WRITE, parallel_safe=True))
        scheduler = ActionScheduler(ToolService(runtime.executor, runtime.context, runtime.repository), max_parallel_reads=4)
        results = await scheduler.execute([ToolCall(str(i), FunctionCall("unknown", "{}")) for i in range(3)],
                                          model_call_id="M", expected_version=runtime.state.version)
        self.assertTrue(all(r.result.success for r in results))
