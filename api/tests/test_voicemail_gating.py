"""Voicemail detection must only be paid for on calls that can reach voicemail.

The classifier gates the main LLM, so enabling it costs a whole extra LLM
round trip in front of the agent's first reply.
"""

from unittest.mock import Mock

import pytest
from pipecat.processors.frame_processor import FrameProcessor

from api.services.pipecat.pipeline_builder import build_pipeline
from api.services.pipecat.voicemail_gating import should_run_voicemail_detection

# transport in/out + PipelineSource/Sink + the 8 processors always present
_BASELINE_PROCESSOR_COUNT = 12

ENABLED = {"enabled": True, "use_workflow_llm": True}


def _check(**overrides):
    kwargs = {
        "is_realtime": False,
        "is_telephony": True,
        "call_type": "outbound",
    }
    kwargs.update(overrides)
    return should_run_voicemail_detection(ENABLED, **kwargs)


def test_runs_on_outbound_telephony():
    assert _check() is True


def test_skipped_on_webrtc_even_when_marked_outbound():
    # Browser calls (agent test panel, embedded widget) are created with the
    # column default call_type='outbound', so the transport is what rules them
    # out — nobody's answering machine picks up a WebRTC offer.
    assert _check(is_telephony=False) is False


def test_skipped_on_inbound_telephony():
    assert _check(call_type="inbound") is False


def test_skipped_when_realtime():
    assert _check(is_realtime=True) is False


@pytest.mark.parametrize("config", [None, {}, {"enabled": False}])
def test_skipped_when_disabled(config):
    assert (
        should_run_voicemail_detection(
            config, is_realtime=False, is_telephony=True, call_type="outbound"
        )
        is False
    )


def test_missing_call_type_is_treated_as_outbound():
    # The column defaults to 'outbound'; a run row that predates the column
    # should keep detection rather than silently lose it.
    assert _check(call_type=None) is True


# --- the flag has to actually keep the gate out of the pipeline ----------------
#
# should_run_voicemail_detection() only decides whether _run_pipeline builds a
# VoicemailDetector. What costs the first turn is the LLMGate that detector
# puts in front of the main LLM, so pin the link between the two.


class _Stub(FrameProcessor):
    """Stand-in for a real pipeline processor."""


class _StubDetector:
    """Minimal stand-in for VoicemailDetector's pipeline-facing surface."""

    def __init__(self):
        self._detector = _Stub()
        self._llm_gate = _Stub()

    def detector(self):
        return self._detector

    def llm_gate(self):
        return self._llm_gate


def _build(voicemail_detector):
    transport = Mock()
    transport.input.return_value = _Stub()
    transport.output.return_value = _Stub()
    return build_pipeline(
        transport=transport,
        stt=_Stub(),
        audio_buffer=_Stub(),
        llm=_Stub(),
        tts=_Stub(),
        user_context_aggregator=_Stub(),
        assistant_context_aggregator=_Stub(),
        pipeline_engine_callback_processor=_Stub(),
        pipeline_metrics_aggregator=_Stub(),
        voicemail_detector=voicemail_detector,
    )


def test_no_detector_means_nothing_sits_in_front_of_the_llm():
    pipeline = _build(None)
    assert len(pipeline._processors) == _BASELINE_PROCESSOR_COUNT


def test_detector_inserts_the_llm_gate_that_costs_the_first_turn():
    detector = _StubDetector()
    processors = _build(detector)._processors
    # The gate is what blocks the conversation LLM until the classifier
    # finishes; removing the detector is what removes that wait.
    assert detector.llm_gate() in processors
    assert detector.detector() in processors
    assert len(processors) == _BASELINE_PROCESSOR_COUNT + 2
