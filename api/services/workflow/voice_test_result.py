"""Strict, non-identifying report for an executed private voice script."""

import math

from api.services.workflow.voice_test_control import VoiceControlError

REHEARSAL_SCOPE = "saved caller PCM to native output speech segments; excludes carrier and network latency"


def validate_rehearsal(value, session):
    if not isinstance(value, dict) or set(value) != {
        "status",
        "turns",
        "measurement_scope",
        "quality_evaluated",
    }:
        raise VoiceControlError("voice_report_invalid")
    if (
        value["status"] != "script_executed"
        or value["quality_evaluated"] is not False
        or value["measurement_scope"] != REHEARSAL_SCOPE
    ):
        raise VoiceControlError("voice_report_invalid")
    turns = value["turns"]
    if (
        session.script is None
        or not isinstance(turns, list)
        or len(turns) != len(session.script.steps)
    ):
        raise VoiceControlError("voice_report_invalid")
    previous = 0
    for turn, step in zip(turns, session.script.steps):
        if not isinstance(turn, dict) or set(turn) != {
            "file_name",
            "delivery",
            "response_sequence",
            "caller_end_to_first_audio_ms",
        }:
            raise VoiceControlError("voice_report_invalid")
        sequence = turn["response_sequence"]
        latency = turn["caller_end_to_first_audio_ms"]
        if (
            turn["file_name"] != step.file_name
            or turn["delivery"] != step.delivery
            or type(sequence) is not int
            or not previous < sequence <= 128
        ):
            raise VoiceControlError("voice_report_invalid")
        if (
            type(latency) not in (int, float)
            or not math.isfinite(latency)
            or abs(latency) > session.duration_seconds * 1000
        ):
            raise VoiceControlError("voice_report_invalid")
        previous = sequence
    return value


def validate_voice_report(value, session, organization_id):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "session_id",
        "run_id",
        "workflow_id",
        "organization_id",
        "runtime_workflow_run_id",
        "workflow_version",
        "version_kind",
        "configuration_hash",
        "runtime_workflow_hash",
        "actual_duration_ms",
        "audio_sha256",
        "audio_bytes",
        "rehearsal",
    }:
        raise VoiceControlError("voice_report_invalid")
    expected = {
        "version": 1,
        "session_id": session.id,
        "run_id": session.run_id,
        "workflow_id": session.workflow_id,
        "organization_id": organization_id,
        "runtime_workflow_run_id": session.runtime_workflow_run_id,
        "workflow_version": session.workflow_version,
        "version_kind": session.version_kind,
        "configuration_hash": session.configuration_hash,
        "runtime_workflow_hash": session.runtime_workflow_hash,
        "actual_duration_ms": session.actual_duration_ms,
        "audio_sha256": session.audio_sha256,
        "audio_bytes": session.audio_bytes,
    }
    if (
        session.status != "completed"
        or session.runtime_workflow_run_id is None
        or session.actual_duration_ms is None
        or session.audio_sha256 is None
        or session.audio_bytes is None
    ):
        raise VoiceControlError("voice_report_not_sealed")
    if any(
        type(value.get(key)) is not type(item) or value.get(key) != item
        for key, item in expected.items()
    ):
        raise VoiceControlError("voice_report_invalid")
    validate_rehearsal(value["rehearsal"], session)
    return value
