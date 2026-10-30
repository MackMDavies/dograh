"""Bind response spans to retained assistant PCM, without speech or tool contents.

These offsets describe the concatenated private output WAV. They are not wall
clock timings, caller audio, transcripts or a linguistic quality assessment.
"""
from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_tool_trace import IDENTITY_KEYS
from api.services.workflow.voice_test_timing_evidence import validate_voice_timing_evidence

AUDIO_EVIDENCE_SCOPE = "private assistant PCM byte spans; excludes caller audio, transcripts, speech quality and carrier timing"
MAX_PCM_BYTES = 120 * 16000 * 2
MAX_SPANS = 128


class PrivateVoiceAudioEvidence:
    def __init__(self):
        self._cursor = 0
        self._spans = []
        self._invalid = False
        self._truncated = False

    def record(self, pcm_start_byte, size, response_sequence):
        if type(pcm_start_byte) is not int or type(size) is not int \
                or pcm_start_byte != self._cursor or size < 0 or size % 2 \
                or pcm_start_byte + size > MAX_PCM_BYTES:
            self._invalid = True
            return
        self._cursor += size
        if size == 0:
            return
        if type(response_sequence) is not int or not 1 <= response_sequence <= MAX_SPANS:
            if type(response_sequence) is int and response_sequence > MAX_SPANS:
                self._truncated = True
            self._invalid = True
            return
        previous = self._spans[-1] if self._spans else None
        if previous and response_sequence < previous["response_sequence"]:
            self._invalid = True
            return
        if previous and response_sequence == previous["response_sequence"]:
            if previous["pcm_end_byte"] == pcm_start_byte:
                previous["pcm_end_byte"] = self._cursor
            else:
                self._invalid = True
            return
        if len(self._spans) == MAX_SPANS:
            self._truncated = True
            return
        self._spans.append({"response_sequence": response_sequence,
                            "pcm_start_byte": pcm_start_byte, "pcm_end_byte": self._cursor})

    def snapshot(self, *, pcm_bytes, responses, pipeline_stopped, timing=None):
        if type(pcm_bytes) is not int or not 0 <= pcm_bytes <= MAX_PCM_BYTES or pcm_bytes % 2:
            raise VoiceControlError("voice_audio_evidence_invalid")
        spans = [dict(span) for span in self._spans]
        invalid = self._invalid or pcm_bytes != self._cursor
        if any(span["pcm_end_byte"] > pcm_bytes for span in spans):
            spans = []
            invalid = True
        covered = sum(span["pcm_end_byte"] - span["pcm_start_byte"] for span in spans)
        expected = [response.sequence for response in responses]
        closed = all(response.first_audio_at is not None and response.stopped_at is not None for response in responses)
        value = {
            "version": 1, "scope": AUDIO_EVIDENCE_SCOPE,
            "complete": bool(pipeline_stopped is True and not invalid and not self._truncated
                             and pcm_bytes and closed and expected
                             and expected == list(range(1, len(expected) + 1))
                             and expected == [span["response_sequence"] for span in spans]
                             and covered == pcm_bytes),
            "truncated": self._truncated, "pcm_bytes": pcm_bytes,
            "unattributed_pcm_bytes": pcm_bytes - covered, "spans": spans,
        }
        if timing is not None:
            value["timing"] = timing
        return value


def make_voice_audio_evidence_report(report, session, evidence):
    value = {"version": 1, **{key: report[key] for key in IDENTITY_KEYS},
             "scenario_id": session.scenario_id, "evidence": evidence}
    return validate_voice_audio_evidence_report(value, report, session)


def validate_voice_audio_evidence_report(value, report, session):
    expected = {"version": 1, **{key: report[key] for key in IDENTITY_KEYS},
                "scenario_id": session.scenario_id}
    if not isinstance(value, dict) or set(value) != set(expected) | {"evidence"}:
        raise VoiceControlError("voice_audio_evidence_unavailable")
    if any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items()):
        raise VoiceControlError("voice_audio_evidence_binding_invalid")
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or set(evidence) - {"timing"} != {
        "version", "scope", "complete", "truncated", "pcm_bytes", "unattributed_pcm_bytes", "spans"
    } or type(evidence["version"]) is not int or evidence["version"] != 1 \
            or evidence["scope"] != AUDIO_EVIDENCE_SCOPE \
            or type(evidence["complete"]) is not bool or type(evidence["truncated"]) is not bool:
        raise VoiceControlError("voice_audio_evidence_invalid")
    pcm_bytes = evidence["pcm_bytes"]
    missing = evidence["unattributed_pcm_bytes"]
    if type(pcm_bytes) is not int or pcm_bytes != report["audio_bytes"] - 44 \
            or not 0 <= pcm_bytes <= MAX_PCM_BYTES or pcm_bytes % 2 \
            or type(missing) is not int or not 0 <= missing <= pcm_bytes or missing % 2 \
            or not isinstance(evidence["spans"], list) or len(evidence["spans"]) > MAX_SPANS:
        raise VoiceControlError("voice_audio_evidence_invalid")
    previous_end, previous_sequence, covered, sequences = 0, 0, 0, set()
    for span in evidence["spans"]:
        if not isinstance(span, dict) or set(span) != {"response_sequence", "pcm_start_byte", "pcm_end_byte"} \
                or any(type(span[key]) is not int for key in span) \
                or not previous_sequence < span["response_sequence"] <= MAX_SPANS \
                or not previous_end <= span["pcm_start_byte"] < span["pcm_end_byte"] <= pcm_bytes \
                or span["pcm_start_byte"] % 2 or span["pcm_end_byte"] % 2:
            raise VoiceControlError("voice_audio_evidence_invalid")
        covered += span["pcm_end_byte"] - span["pcm_start_byte"]
        previous_end, previous_sequence = span["pcm_end_byte"], span["response_sequence"]
        sequences.add(previous_sequence)
    required = {turn["response_sequence"] for turn in report["rehearsal"]["turns"]}
    if covered + missing != pcm_bytes or evidence["complete"] and (
        evidence["truncated"] or missing or not pcm_bytes or not required <= sequences
        or [span["response_sequence"] for span in evidence["spans"]] != list(range(1, len(evidence["spans"]) + 1))
    ):
        raise VoiceControlError("voice_audio_evidence_invalid")
    if "timing" in evidence:
        validate_voice_timing_evidence(evidence["timing"], duration_ms=report["actual_duration_ms"],
                                      fixtures=session.fixtures, spans=evidence["spans"])
    return value
