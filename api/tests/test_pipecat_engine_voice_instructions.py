"""Call-start constructor compatibility and per-node voice policy composition."""
import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services.workflow.dto import Position, ReactFlowDTO, RFNodeDTO, StartCallNodeData
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph


def workflow():
    return WorkflowGraph(ReactFlowDTO(nodes=[RFNodeDTO(
        id="start", type="startCall", position=Position(x=0, y=0),
        data=StartCallNodeData(name="Start", prompt="Help book an appointment.", is_start=True),
    )], edges=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("is_realtime", [False, True])
async def test_call_start_accepts_voice_policy_arguments_and_applies_them_on_every_node(is_realtime):
    llm = SimpleNamespace(_context=None, _update_settings=AsyncMock(), register_function=Mock())
    context = LLMContext()
    engine = PipecatEngine(
        workflow=workflow(), call_context_vars={}, llm=llm, context=context,
        voice_expression_instruction="Use warm audio tags where appropriate.",
        guardrail_instruction="Never disclose private access codes.",
        is_realtime=is_realtime,
    )
    engine._get_organization_id = AsyncMock(return_value=None)
    engine._get_workflow_id = AsyncMock(return_value=None)
    await engine.initialize()
    await engine.set_node("start")
    await engine.set_node("start")
    assert llm._context is context
    assert engine._is_realtime is is_realtime
    assert llm._update_settings.await_count == 2
    for call in llm._update_settings.call_args_list:
        prompt = call.args[0].system_instruction
        assert "Help book an appointment." in prompt
        assert prompt.count("Use warm audio tags where appropriate.") == 1
        assert prompt.count("Never disclose private access codes.") == 1


def test_pipeline_engine_constructor_keywords_match_the_engine_signature():
    source = Path(__file__).parents[1] / "services/pipecat/run_pipeline.py"
    tree = ast.parse(source.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "PipecatEngine"]
    assert calls
    signature = inspect.signature(PipecatEngine)
    for call in calls:
        arguments = {keyword.arg: None for keyword in call.keywords if keyword.arg}
        signature.bind(**arguments)
