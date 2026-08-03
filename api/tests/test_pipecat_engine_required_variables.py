"""Tests for the `required_for_exit` transition gate.

An extraction variable marked `required_for_exit=True` must be present
(non-empty) in `_gathered_context` before the node that declares it can be
left via any outgoing transition. This is the enforcement mechanism behind
"the agent must collect phone/email before it can book an appointment" —
previously a node's outgoing transitions were unconditional LLM function
calls with no such gate.
"""

from unittest.mock import AsyncMock

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
    _MAX_REQUIRED_VARIABLE_RETRIES,
    PipecatEngine,
)
from api.services.workflow.pipecat_engine_variable_extractor import (
    VariableExtractionManager,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow_with_required_contact_fields() -> WorkflowGraph:
    """Start node requires `phone` and `email` before its outgoing edge fires."""
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="collect_contact",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Collect Contact",
                    prompt="Ask for the caller's phone number and email.",
                    is_start=True,
                    extraction_enabled=True,
                    extraction_prompt="Extract phone and email from the conversation.",
                    extraction_variables=[
                        ExtractionVariableDTO(
                            name="phone",
                            type=VariableType.string,
                            prompt="The caller's phone number",
                            required_for_exit=True,
                        ),
                        ExtractionVariableDTO(
                            name="email",
                            type=VariableType.string,
                            prompt="The caller's email address",
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
                    name="Book Appointment",
                    prompt="Confirm the appointment.",
                    extraction_enabled=False,
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=400),
                data=EndCallNodeData(
                    name="End Call",
                    prompt="End the call.",
                    is_end=True,
                    extraction_enabled=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="contact-book",
                source="collect_contact",
                target="book",
                data=EdgeDataDTO(label="Proceed to Booking", condition="Contact captured"),
            ),
            RFEdgeDTO(
                id="book-end",
                source="book",
                target="end",
                data=EdgeDataDTO(label="End Call", condition="Booking confirmed"),
            ),
        ],
    )
    return WorkflowGraph(dto)


def _make_engine() -> PipecatEngine:
    workflow = _workflow_with_required_contact_fields()
    engine = PipecatEngine(
        workflow=workflow,
        call_context_vars={},
        workflow_run_id=1,
    )
    engine._current_node = workflow.nodes["collect_contact"]
    return engine


def _fake_function_call_params() -> AsyncMock:
    params = AsyncMock()
    params.result_callback = AsyncMock()
    return params


class TestRequiredVariableGate:
    @pytest.mark.asyncio
    async def test_blocks_transition_when_required_variables_missing(self):
        engine = _make_engine()
        params = _fake_function_call_params()

        async def extraction_returns_nothing(*args, **kwargs):
            return None

        engine._variable_extraction_manager = None  # not used directly
        from unittest.mock import patch

        with patch.object(
            VariableExtractionManager,
            "_perform_extraction",
            new_callable=AsyncMock,
            return_value={},
        ):
            engine._variable_extraction_manager = VariableExtractionManager(engine)
            proceed = await engine._enforce_required_variables_before_exit(
                "proceed_to_booking", params
            )

        assert proceed is False
        params.result_callback.assert_awaited_once()
        (result,), _ = params.result_callback.call_args
        assert result["status"] == "blocked"
        assert "phone" in result["reason"] and "email" in result["reason"]

    @pytest.mark.asyncio
    async def test_allows_transition_when_required_variables_present(self):
        engine = _make_engine()
        params = _fake_function_call_params()

        from unittest.mock import patch

        with patch.object(
            VariableExtractionManager,
            "_perform_extraction",
            new_callable=AsyncMock,
            return_value={"phone": "+15551234567", "email": "graham@example.com"},
        ):
            engine._variable_extraction_manager = VariableExtractionManager(engine)
            proceed = await engine._enforce_required_variables_before_exit(
                "proceed_to_booking", params
            )

        assert proceed is True
        params.result_callback.assert_not_awaited()
        assert engine._gathered_context["phone"] == "+15551234567"
        assert engine._gathered_context["email"] == "graham@example.com"

    @pytest.mark.asyncio
    async def test_partial_capture_still_blocks(self):
        """Only phone captured — email still missing — must still block."""
        engine = _make_engine()
        params = _fake_function_call_params()

        from unittest.mock import patch

        with patch.object(
            VariableExtractionManager,
            "_perform_extraction",
            new_callable=AsyncMock,
            return_value={"phone": "+15551234567", "email": ""},
        ):
            engine._variable_extraction_manager = VariableExtractionManager(engine)
            proceed = await engine._enforce_required_variables_before_exit(
                "proceed_to_booking", params
            )

        assert proceed is False
        (result,), _ = params.result_callback.call_args
        assert "email" in result["reason"]
        assert "phone" not in result["reason"]

    @pytest.mark.asyncio
    async def test_retry_cap_eventually_lets_transition_through(self):
        """After enough blocked attempts, don't trap the call forever."""
        engine = _make_engine()

        from unittest.mock import patch

        with patch.object(
            VariableExtractionManager,
            "_perform_extraction",
            new_callable=AsyncMock,
            return_value={},
        ):
            engine._variable_extraction_manager = VariableExtractionManager(engine)

            results = []
            for _ in range(_MAX_REQUIRED_VARIABLE_RETRIES + 1):
                params = _fake_function_call_params()
                results.append(
                    await engine._enforce_required_variables_before_exit(
                        "proceed_to_booking", params
                    )
                )

        # Every attempt within budget is blocked; the final one is let through.
        assert results[:-1] == [False] * _MAX_REQUIRED_VARIABLE_RETRIES
        assert results[-1] is True
        assert engine._gathered_context["required_variables_unresolved"][0][
            "node"
        ] == "Collect Contact"
        # The counter is cleared once the node is successfully exited.
        assert "collect_contact" not in engine._transition_block_counts

    @pytest.mark.asyncio
    async def test_node_without_required_variables_is_unaffected(self):
        """A node with no `required_for_exit` variables keeps prior behavior:
        extraction is scheduled in the background and the transition proceeds
        immediately without waiting on it."""
        engine = _make_engine()
        engine._current_node = engine.workflow.nodes["book"]  # extraction disabled
        params = _fake_function_call_params()

        proceed = await engine._enforce_required_variables_before_exit(
            "end_call", params
        )

        assert proceed is True
        params.result_callback.assert_not_awaited()
