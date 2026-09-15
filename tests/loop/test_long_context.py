import json
import unittest

from qharness.exception import LoopConfigurationError, LoopExecutionError
from qharness.loop.config import LoopConfig, Role
from qharness.loop.context import ContextCompiler, validate_messages
from qharness.loop.models import StageOutcome
from qharness.loop.repository import digest
from qharness.model.models import ChatMessage, FunctionCall, ToolCall
from tests.loop.test_transitions import begin
from tests.support.loop_runtime import LoopRuntime


class LongContextTests(unittest.TestCase):
    def history(self):
        return [m for i in range(18) for m in (
            ChatMessage("assistant", tool_calls=[ToolCall(f"c{i}", FunctionCall("read_file", '{"path":"file.py"}'))]),
            ChatMessage("tool", "previous output " * 1500, tool_call_id=f"c{i}"))]

    def test_compaction_keeps_contract_questions_stage_and_complete_tool_pairs(self):
        state = begin()
        history = self.history()
        compiler = ContextCompiler(LoopConfig(context_compaction=True, max_input_bytes=24000,
                                              context_window_tokens=30000, context_keep_turns=2))
        request, tokens = compiler.compile(contract=state.contract, role=Role.ACTOR, output_schema=StageOutcome,
            state=state, todo_id="T1", stage=state.attempts[-1].plan, messages=history,
            observations=("oversized irrelevant log " * 10000,))
        payload = json.loads(request.messages[1].content)
        self.assertEqual(payload["task"], state.contract.model_dump(mode="json"))
        self.assertEqual(payload["stage"], state.attempts[-1].plan.model_dump(mode="json"))
        self.assertEqual(payload["run_state"]["questions"], [q.model_dump(mode="json") for q in state.questions])
        self.assertIn("context_manifest", payload)
        self.assertEqual(len(request.messages[2:]), 4)
        validate_messages(request.messages[2:])
        self.assertLess(tokens, 26000)
        self.assertEqual(len(history[-1].content), len("previous output " * 1500))
        ref, source, manifest = compiler.compacted_source
        self.assertEqual(len(source["messages"]), 38)
        self.assertEqual(manifest["source_ref"], ref)

    def test_compression_is_deterministic_and_does_not_shrink_required_contract(self):
        state = begin()
        compiler = ContextCompiler(LoopConfig(context_compaction=True, max_input_bytes=24000,
                                              context_window_tokens=30000, context_keep_turns=2))
        args = dict(contract=state.contract, role=Role.ACTOR, output_schema=StageOutcome,
                    state=state, todo_id="T1", stage=state.attempts[-1].plan, messages=self.history())
        first, _ = compiler.compile(**args)
        first_ref = compiler.compacted_source[0]
        second, _ = compiler.compile(**args)
        self.assertEqual(first.messages, second.messages)
        self.assertEqual(first_ref, compiler.compacted_source[0])
        tiny = ContextCompiler(LoopConfig(context_compaction=True, max_input_bytes=100))
        with self.assertRaises(LoopConfigurationError):
            tiny.compile(**args)

    def test_disabled_compaction_retains_original_capacity_failure(self):
        state = begin()
        with self.assertRaises(LoopConfigurationError):
            ContextCompiler(LoopConfig(max_input_bytes=24000)).compile(contract=state.contract, role=Role.ACTOR,
                output_schema=StageOutcome, state=state, todo_id="T1", stage=state.attempts[-1].plan, messages=self.history())

    def test_artifact_budget_preserves_referenced_records_and_idempotent_writes(self):
        r = LoopRuntime(config=LoopConfig(max_artifact_bytes=100))
        self.addCleanup(r.close)
        ref = digest(["protected"])
        r.repository.put_artifact(ref, {"value": "原始凭据"})
        r.repository.put_artifact(ref, {"value": "原始凭据"})
        with self.assertRaises(LoopExecutionError) as error:
            r.repository.put_artifact(digest(["huge"]), {"value": "a" * 101})
        self.assertEqual(error.exception.code, "artifact_quota")
        self.assertEqual(r.repository.read_artifact(ref), {"value": "原始凭据"})
        with self.assertRaises(LoopExecutionError):
            r.repository.put_artifact(ref, {"value": "改写凭据"})

    def test_stage_guidance_and_check_catalog_are_pinned(self):
        state = begin()
        compiler = ContextCompiler(LoopConfig(context_compaction=True, max_input_bytes=24000,
                                              context_window_tokens=30000, context_keep_turns=1))
        guide = json.dumps({"stage_guidance": {"kind": "investigate", "focus": ["Q1"]}})
        catalog = json.dumps({"available_checks": [{"id": "trusted-check", "targets": ["C1"]}]})
        request, _ = compiler.compile(contract=state.contract, role=Role.ACTOR, output_schema=StageOutcome,
            state=state, todo_id="T1", stage=state.attempts[-1].plan, messages=self.history(),
            observations=(guide, catalog, *["noise " * 5000 for _ in range(8)]))
        observations = json.loads(request.messages[1].content)["observations"]
        self.assertIn(guide, observations)
        self.assertIn(catalog, observations)


class ContextManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_call_persists_complete_source_and_manifest_before_dispatch(self):
        from qharness.loop.model_service import ModelService
        from tests.loop.test_actor import handback
        from tests.support.scripted_backend import ScriptedModelBackend
        state = begin()
        config = LoopConfig(context_compaction=True, max_input_bytes=24000, context_window_tokens=30000, context_keep_turns=2)
        r = LoopRuntime(config=config, state=state)
        self.addCleanup(r.close)
        backend = ScriptedModelBackend([handback(state)])
        model = ModelService(backend, r.repository, ContextCompiler(config))
        result = await model.call("compressed", Role.ACTOR, messages=LongContextTests().history(), expected_version=state.version)
        manifest = r.repository.read_artifact(digest(["context-manifest", "compressed"]))
        source = r.repository.read_artifact(manifest["source_ref"])
        self.assertEqual(len(source["messages"]), 38)
        self.assertEqual(result.output.attempt_id, state.active_attempt_id)
        self.assertEqual(r.repository.snapshot()["budget"]["model_attempts"], 1)
        self.assertEqual(r.repository.snapshot()["state"], state)
        self.assertEqual(json.loads(backend.requests[0].messages[1].content)["context_manifest"], manifest)
