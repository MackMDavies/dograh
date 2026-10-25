"""Bounded private model-dispatch evidence; never inspect arguments or results.

This wraps the per-session LLM entry point before the pipeline starts. Unlike
asynchronous frame observers, recording finishes before dispatch can begin.
It proves observed tool dispatch attempts, not provider effects or speech quality.
"""

import re

from api.services.workflow.voice_test_control import VoiceControlError

TOOL_TRACE_SCOPE = "private native model tool dispatch attempts; excludes tool values, provider effects and speech evaluation"
MAX_TOOL_CALLS = 64
IDENTITY_KEYS = {
    "session_id", "run_id", "workflow_id", "organization_id",
    "runtime_workflow_run_id", "workflow_version", "version_kind",
    "configuration_hash", "runtime_workflow_hash", "actual_duration_ms",
    "audio_sha256", "audio_bytes",
}


class PrivateVoiceToolTrace:
    def __init__(self, caller_files):
        self._caller_files = tuple(caller_files)
        self._step = 0
        self._calls = []
        self._invalid = False
        self._truncated = False
        self._service = None
        self._hook = None

    def attach(self, service):
        from pipecat.services.llm_service import LLMService

        if self._service is not None or not isinstance(service, LLMService) \
                or getattr(service, "_sysevo_private_tool_trace", None) is not None:
            raise VoiceControlError("voice_tool_trace_attachment_invalid")
        original = service.run_function_calls

        async def traced(function_calls):
            self._record(function_calls)
            return await original(function_calls)

        service.run_function_calls = traced
        service._sysevo_private_tool_trace = self
        self._hook = traced
        self._service = service

    def begin_caller_step(self, file_name):
        if self._step >= len(self._caller_files) or self._caller_files[self._step] != file_name:
            self._invalid = True
            return
        self._step += 1

    def _record(self, calls):
        if not isinstance(calls, (list, tuple)):
            self._invalid = True
            return
        if len(calls) > MAX_TOOL_CALLS - len(self._calls):
            self._truncated = True
        for call in calls[:MAX_TOOL_CALLS - len(self._calls)]:
            name = getattr(call, "function_name", None)
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", name) is None:
                name = None
                self._invalid = True
            self._calls.append({
                "sequence": len(self._calls) + 1,
                "caller_step": self._step,
                "function_name": name,
            })

    def snapshot(self, *, pipeline_stopped):
        attached = self._service is not None and self._service.run_function_calls is self._hook \
            and self._service._sysevo_private_tool_trace is self
        return {
            "version": 1,
            "scope": TOOL_TRACE_SCOPE,
            "complete": bool(attached and pipeline_stopped and not self._invalid
                             and not self._truncated and self._step == len(self._caller_files)),
            "truncated": self._truncated,
            "caller_steps_observed": self._step,
            "calls": [dict(call) for call in self._calls],
            "raw_values_retained": False,
        }


def make_voice_tool_trace_report(report, session, trace):
    return {"version": 1, **{key: report[key] for key in IDENTITY_KEYS},
            "scenario_id": session.scenario_id, "trace": trace}


def validate_voice_tool_trace_report(value, report, session):
    expected = {"version": 1, **{key: report[key] for key in IDENTITY_KEYS},
                "scenario_id": session.scenario_id}
    if not isinstance(value, dict) or set(value) != set(expected) | {"trace"}:
        raise VoiceControlError("voice_tool_trace_unavailable")
    if any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items()):
        raise VoiceControlError("voice_tool_trace_binding_invalid")
    trace = value["trace"]
    if not isinstance(trace, dict) or set(trace) != {
        "version", "scope", "complete", "truncated", "caller_steps_observed", "calls", "raw_values_retained"
    } or type(trace["version"]) is not int or trace["version"] != 1 or trace["scope"] != TOOL_TRACE_SCOPE \
            or type(trace["complete"]) is not bool or type(trace["truncated"]) is not bool \
            or trace["raw_values_retained"] is not False:
        raise VoiceControlError("voice_tool_trace_invalid")
    step_count = len(session.script.steps)
    if type(trace["caller_steps_observed"]) is not int or not 0 <= trace["caller_steps_observed"] <= step_count \
            or not isinstance(trace["calls"], list) or len(trace["calls"]) > MAX_TOOL_CALLS:
        raise VoiceControlError("voice_tool_trace_invalid")
    previous_step = 0
    for index, call in enumerate(trace["calls"]):
        if not isinstance(call, dict) or set(call) != {"sequence", "caller_step", "function_name"} \
                or type(call["sequence"]) is not int or call["sequence"] != index + 1 \
                or type(call["caller_step"]) is not int \
                or not previous_step <= call["caller_step"] <= trace["caller_steps_observed"]:
            raise VoiceControlError("voice_tool_trace_invalid")
        name = call["function_name"]
        if name is not None and (not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", name) is None):
            raise VoiceControlError("voice_tool_trace_invalid")
        previous_step = call["caller_step"]
    if trace["complete"] and (trace["truncated"] or trace["caller_steps_observed"] != step_count
                              or any(call["function_name"] is None for call in trace["calls"])):
        raise VoiceControlError("voice_tool_trace_invalid")
    return value
