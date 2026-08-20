"""The agent must not go silent during work that produces no speech.

Three stretches of a turn emit nothing on their own:

  - the second completion a node transition costs (tool call, then a fresh
    completion against the new node's system prompt),
  - the synchronous extraction a node with `required_for_exit` variables runs
    inside the transition, before it will let the transition through,
  - a knowledge-base retrieval: an embedding call, a vector search, and then
    another completion.

`transition_filler_enabled` covers them with a short holding phrase. These
tests pin *when* it is spoken, because a filler queued after the wait it is
meant to cover is worth nothing.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    EndCallNodeData,
    ExtractionVariableDTO,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
    VariableType,
)
from api.services.workflow.pipecat_engine import (
    LOOKUP_FILLERS,
    TRANSITION_FILLERS,
    PipecatEngine,
)
from api.services.workflow.pipecat_engine_variable_extractor import (
    VariableExtractionManager,
)
from api.services.workflow.workflow_graph import WorkflowGraph

AUTHORED_SPEECH = "Putting you through now."


def _workflow(*, authored_speech: str | None = None) -> WorkflowGraph:
    """Start node requires `phone` before its outgoing edge may fire."""
    return WorkflowGraph(
        ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="collect",
                    type="startCall",
                    position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Collect Contact",
                        prompt="Ask for the caller's phone number.",
                        is_start=True,
                        extraction_enabled=True,
                        extraction_prompt="Extract the phone number.",
                        extraction_variables=[
                            ExtractionVariableDTO(
                                name="phone",
                                type=VariableType.string,
                                prompt="The caller's phone number",
                                required_for_exit=True,
                            ),
                        ],
                    ),
                ),
                RFNodeDTO(
                    id="book",
                    type="agentNode",
                    position=Position(x=0, y=200),
                    data=AgentNodeData(
                        name="Book", prompt="Confirm.", extraction_enabled=False
                    ),
                ),
                RFNodeDTO(
                    id="end",
                    type="endCall",
                    position=Position(x=0, y=400),
                    data=EndCallNodeData(
                        name="End", prompt="Bye.", is_end=True, extraction_enabled=False
                    ),
                ),
            ],
            edges=[
                RFEdgeDTO(
                    id="collect-book",
                    source="collect",
                    target="book",
                    data=EdgeDataDTO(
                        label="Proceed",
                        condition="Phone captured",
                        transition_speech=authored_speech,
                    ),
                ),
                RFEdgeDTO(
                    id="book-end",
                    source="book",
                    target="end",
                    data=EdgeDataDTO(label="End", condition="Done"),
                ),
            ],
        )
    )


class _Recorder:
    """Records the order of the things that happen during a turn."""

    def __init__(self):
        self.timeline: list[str] = []
        self.spoken: list[str] = []

    async def queue_frame(self, frame):
        text = getattr(frame, "text", None)
        if text is not None:
            self.spoken.append(text)
            self.timeline.append("speech")


def _engine(*, authored_speech=None, filler_enabled=True):
    workflow = _workflow(authored_speech=authored_speech)
    recorder = _Recorder()
    task = Mock()
    task.queue_frame = recorder.queue_frame
    engine = PipecatEngine(
        task=task,
        llm=Mock(),
        workflow=workflow,
        call_context_vars={},
        workflow_run_id=1,
        transition_filler_enabled=filler_enabled,
    )
    engine._current_node = workflow.nodes["collect"]
    engine.set_node = AsyncMock()  # the transition itself is not under test
    return engine, recorder


def _params():
    params = AsyncMock()
    params.result_callback = AsyncMock()
    return params


async def _run_transition(engine, recorder, *, extracted):
    async def _extract(*args, **kwargs):
        recorder.timeline.append("extraction")
        return extracted

    edge = engine.workflow.nodes["collect"].out_edges[0]
    func = await engine._create_transition_func(
        "proceed",
        "book",
        edge.transition_speech,
        edge.data.transition_speech_type,
        edge.data.transition_speech_recording_id,
    )
    with patch.object(
        VariableExtractionManager, "_perform_extraction", new=_extract
    ):
        engine._variable_extraction_manager = VariableExtractionManager(engine)
        await func(_params())


class TestTransitionFiller:
    @pytest.mark.asyncio
    async def test_filler_is_spoken_before_the_blocking_extraction(self):
        # The required-variable gate awaits extraction synchronously. A filler
        # queued after it would leave that whole wait silent.
        engine, rec = _engine()
        await _run_transition(engine, rec, extracted={"phone": "+15551234567"})

        assert rec.timeline == ["speech", "extraction"]
        assert rec.spoken == [TRANSITION_FILLERS[0]]

    @pytest.mark.asyncio
    async def test_caller_still_hears_something_when_the_gate_blocks(self):
        engine, rec = _engine()
        await _run_transition(engine, rec, extracted={})

        engine.set_node.assert_not_awaited()
        assert rec.spoken == [TRANSITION_FILLERS[0]]

    @pytest.mark.asyncio
    async def test_authored_speech_waits_for_the_gate(self):
        # Authored speech can promise the transition, so it must not be spoken
        # until the gate has actually allowed one.
        engine, rec = _engine(authored_speech=AUTHORED_SPEECH)
        await _run_transition(engine, rec, extracted={})

        assert rec.spoken == []

    @pytest.mark.asyncio
    async def test_authored_speech_is_spoken_once_the_gate_allows(self):
        engine, rec = _engine(authored_speech=AUTHORED_SPEECH)
        await _run_transition(engine, rec, extracted={"phone": "+15551234567"})

        # Authored speech replaces the filler — it is not spoken on top of it.
        assert rec.spoken == [AUTHORED_SPEECH]

    @pytest.mark.asyncio
    async def test_nothing_is_spoken_when_the_workflow_has_not_opted_in(self):
        engine, rec = _engine(filler_enabled=False)
        await _run_transition(engine, rec, extracted={"phone": "+15551234567"})

        assert rec.spoken == []
        assert rec.timeline == ["extraction"]

    @pytest.mark.asyncio
    async def test_fillers_rotate_rather_than_repeat(self):
        # One transition per caller turn, as on a real call — consecutive
        # transitions inside a single turn are deliberately bridged only once
        # (see TestOneFillerPerTurn).
        engine, rec = _engine()
        for _ in range(3):
            engine.begin_user_turn()
            engine._current_node = engine.workflow.nodes["collect"]
            await _run_transition(engine, rec, extracted={"phone": "+1555"})

        assert rec.spoken == list(TRANSITION_FILLERS[:3])


class TestKnowledgeBaseFiller:
    @pytest.mark.asyncio
    async def test_lookup_is_announced_before_the_retrieval_runs(self):
        engine, rec = _engine()

        registered = {}
        engine.llm.register_function = lambda name, fn: registered.update({name: fn})
        await engine._register_knowledge_base_function(["doc-uuid"])

        async def _retrieve(*args, **kwargs):
            rec.timeline.append("retrieval")
            return {"chunks": [], "total_results": 0}

        with patch(
            "api.services.workflow.pipecat_engine.retrieve_from_knowledge_base",
            new=_retrieve,
        ), patch.object(
            PipecatEngine, "_get_organization_id", new=AsyncMock(return_value=1)
        ):
            params = _params()
            params.arguments = {"query": "what does it cost"}
            await registered["retrieve_from_knowledge_base"](params)

        assert rec.timeline == ["speech", "retrieval"]
        assert rec.spoken == [LOOKUP_FILLERS[0]]

    @pytest.mark.asyncio
    async def test_lookup_filler_is_distinct_from_the_transition_filler(self):
        # "Got it." after a question the agent has not answered yet reads as an
        # answer; a lookup needs to say it is looking.
        assert not set(LOOKUP_FILLERS) & set(TRANSITION_FILLERS)


class TestPerCallCaching:
    @pytest.mark.asyncio
    async def test_workflow_documents_are_looked_up_once_per_call(self):
        """_setup_llm_context runs on every transition, and this lookup takes
        the same two arguments every time — so it must not put a database
        round trip between the transition's two completions."""
        engine, _ = _engine()
        engine.context = Mock()
        engine.llm.register_function = Mock()
        engine._update_llm_context = AsyncMock()
        engine._get_workflow_id = AsyncMock(return_value=7)
        engine._get_organization_id = AsyncMock(return_value=1)

        lookup = AsyncMock(return_value=["doc-a", "doc-b"])
        with patch(
            "api.services.workflow.pipecat_engine.db_client.get_document_uuids_for_workflow",
            new=lookup,
        ):
            for node_id in ("collect", "book", "collect"):
                await engine._setup_llm_context(engine.workflow.nodes[node_id])

        assert lookup.await_count == 1

    @pytest.mark.asyncio
    async def test_cached_documents_are_not_mutated_across_nodes(self):
        """Node-level uuids get appended to the workflow-level list, so handing
        out the cached list itself would grow it on every transition."""
        engine, _ = _engine()
        engine.context = Mock()
        engine.llm.register_function = Mock()
        engine._update_llm_context = AsyncMock()
        engine._get_workflow_id = AsyncMock(return_value=7)
        engine._get_organization_id = AsyncMock(return_value=1)

        captured: list[list[str]] = []

        async def _capture(*, node, custom_tool_manager, kb_document_uuids):
            captured.append(list(kb_document_uuids or []))
            return []

        with patch(
            "api.services.workflow.pipecat_engine.db_client.get_document_uuids_for_workflow",
            new=AsyncMock(return_value=["doc-a"]),
        ), patch(
            "api.services.workflow.pipecat_engine.compose_functions_for_node",
            new=_capture,
        ):
            for node_id in ("collect", "book", "collect"):
                await engine._setup_llm_context(engine.workflow.nodes[node_id])

        assert captured == [["doc-a"], ["doc-a"], ["doc-a"]]
        assert engine._workflow_document_uuids == ["doc-a"]


class TestOneFillerPerTurn:
    """Two transitions can fire back to back with no caller turn between them.

    Each one speaking produced "Got it. Okay, sure." on a live call (run 2801)
    — which reads as a stall, the opposite of what a filler is for.
    """

    @pytest.mark.asyncio
    async def test_consecutive_transitions_only_bridge_once(self):
        engine, rec = _engine()
        engine.begin_user_turn()
        await engine._speak_filler(TRANSITION_FILLERS, "_transition_filler_index")
        await engine._speak_filler(TRANSITION_FILLERS, "_transition_filler_index")
        await engine._speak_filler(LOOKUP_FILLERS, "_lookup_filler_index")

        assert rec.spoken == [TRANSITION_FILLERS[0]]

    @pytest.mark.asyncio
    async def test_the_next_caller_turn_gets_its_own_bridge(self):
        engine, rec = _engine()
        engine.begin_user_turn()
        await engine._speak_filler(TRANSITION_FILLERS, "_transition_filler_index")
        engine.begin_user_turn()
        await engine._speak_filler(TRANSITION_FILLERS, "_transition_filler_index")

        assert rec.spoken == list(TRANSITION_FILLERS[:2])

    @pytest.mark.asyncio
    async def test_a_knowledge_base_lookup_still_speaks_after_a_transition_turn(self):
        # Different turns, so the lookup is not suppressed by the transition.
        engine, rec = _engine()
        engine.begin_user_turn()
        await engine._speak_filler(TRANSITION_FILLERS, "_transition_filler_index")
        engine.begin_user_turn()
        await engine._speak_filler(LOOKUP_FILLERS, "_lookup_filler_index")

        assert rec.spoken == [TRANSITION_FILLERS[0], LOOKUP_FILLERS[0]]
