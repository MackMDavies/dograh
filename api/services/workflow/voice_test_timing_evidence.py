"""Bounded native PCM submission/playback times, with no audio or text contents."""
import math

from api.services.workflow.voice_test_control import VoiceControlError

TIMING_SCOPE = "native monotonic PCM delivery; excludes phonetic pauses, carrier and network latency"
RESPONSE_KEYS = {"response_sequence", "pcm_bytes", "started_ms", "first_pcm_ms", "last_pcm_end_ms",
                 "stopped_ms", "delivery_gap_count", "max_delivery_gap_ms", "total_delivery_gap_ms"}
CALLER_KEYS = {"file_name", "sha256", "pcm_bytes", "submitted_pcm_bytes", "first_frame_ms",
               "last_frame_end_ms", "playback_finished_ms"}


class PrivateVoiceTimingEvidence:
    def __init__(self, responses):
        self.responses = responses
        self.origin = None
        self.audio = {}
        self.callers = []
        self.invalid = False

    def observe(self, at):
        if type(at) not in (int, float) or not math.isfinite(at):
            self.invalid = True
            return
        if self.origin is None:
            self.origin = at
        if at < self.origin:
            self.invalid = True

    def record_output(self, at, size, sequence):
        self.observe(at)
        if not size:
            return
        if type(sequence) is not int or not 1 <= sequence <= 128 or type(size) is not int or size < 0 or size % 2:
            self.invalid = True
            return
        item = self.audio.get(sequence)
        if item is None:
            item = self.audio[sequence] = dict(first=at, end=at, pcm_bytes=0, gaps=0, maximum=0, total=0)
        elif at < item["end"] - 0.00001:
            self.invalid = True
        gap_ms = (at - item["end"]) * 1000
        if item["pcm_bytes"] and gap_ms > 20:
            item["gaps"] += 1
            item["maximum"] = max(item["maximum"], gap_ms)
            item["total"] += gap_ms
        item["end"] = at + size / 32000
        item["pcm_bytes"] += size

    def begin_caller(self, file_name, sha256, pcm_bytes):
        if len(self.callers) >= 10 or any(item["file_name"] == file_name for item in self.callers):
            self.invalid = True
            return None
        item = dict(file_name=file_name, sha256=sha256, pcm_bytes=pcm_bytes, submitted_pcm_bytes=0,
                    first=None, end=None, finished=None)
        self.callers.append(item)
        return item

    def record_input(self, item, at, offset, size, padded_size):
        self.observe(at)
        if item is None:
            self.invalid = True
            return
        if item["submitted_pcm_bytes"] != offset or not 0 < size <= padded_size or size % 2 or padded_size != 640:
            self.invalid = True
            return
        if item["first"] is None:
            item["first"] = at
        item["end"] = at + padded_size / 32000
        item["submitted_pcm_bytes"] += size

    def finish_input(self, item, at):
        self.observe(at)
        if item is None:
            self.invalid = True
            return
        item["finished"] = at

    def snapshot(self, *, pipeline_stopped):
        result = dict(version=1, scope=TIMING_SCOPE, complete=False, caller_steps=[], responses=[])
        if self.origin is None:
            return result
        relative = lambda at: round((at - self.origin) * 1000, 3)
        valid = not self.invalid
        for response in self.responses:
            audio = self.audio.get(response.sequence)
            if audio is None or response.stopped_at is None:
                valid = False
                continue
            result["responses"].append(dict(
                response_sequence=response.sequence, pcm_bytes=audio["pcm_bytes"],
                started_ms=relative(response.started_at), first_pcm_ms=relative(audio["first"]),
                last_pcm_end_ms=relative(audio["end"]), stopped_ms=relative(response.stopped_at),
                delivery_gap_count=audio["gaps"], max_delivery_gap_ms=round(audio["maximum"], 3),
                total_delivery_gap_ms=round(audio["total"], 3),
            ))
        for item in self.callers:
            if any(item[key] is None for key in ("first", "end", "finished")):
                valid = False
                continue
            result["caller_steps"].append({key: item[key] for key in
                ("file_name", "sha256", "pcm_bytes", "submitted_pcm_bytes")} | dict(
                first_frame_ms=relative(item["first"]), last_frame_end_ms=relative(item["end"]),
                playback_finished_ms=relative(item["finished"]),
            ))
            valid = valid and item["submitted_pcm_bytes"] == item["pcm_bytes"]
        result["complete"] = bool(pipeline_stopped is True and valid and self.responses and self.callers
                                  and len(self.audio) == len(self.responses))
        return result


def validate_voice_timing_evidence(value, *, duration_ms, fixtures, spans):
    def invalid():
        raise VoiceControlError("voice_timing_evidence_invalid")

    def number(item):
        return type(item) in (int, float) and math.isfinite(item) and 0 <= item <= duration_ms

    if not isinstance(value, dict) or set(value) != {"version", "scope", "complete", "caller_steps", "responses"} \
            or type(value["version"]) is not int or value["version"] != 1 or value["scope"] != TIMING_SCOPE \
            or type(value["complete"]) is not bool or not isinstance(value["caller_steps"], list) \
            or len(value["caller_steps"]) > len(fixtures) or not isinstance(value["responses"], list) \
            or len(value["responses"]) > 128:
        invalid()
    spans_by_id = {item["response_sequence"]: item["pcm_end_byte"] - item["pcm_start_byte"] for item in spans}
    previous_sequence, previous_stop = 0, 0
    for response in value["responses"]:
        if not isinstance(response, dict) or set(response) != RESPONSE_KEYS \
                or type(response["response_sequence"]) is not int or not previous_sequence < response["response_sequence"] <= 128 \
                or type(response["pcm_bytes"]) is not int or response["pcm_bytes"] != spans_by_id.get(response["response_sequence"]) \
                or type(response["delivery_gap_count"]) is not int or not 0 <= response["delivery_gap_count"] <= response["pcm_bytes"] / 2 \
                or any(not number(response[key]) for key in ("started_ms", "first_pcm_ms", "last_pcm_end_ms", "stopped_ms", "max_delivery_gap_ms", "total_delivery_gap_ms")):
            invalid()
        start, first, end, stop = (response[key] for key in ("started_ms", "first_pcm_ms", "last_pcm_end_ms", "stopped_ms"))
        count, maximum, total = (response[key] for key in ("delivery_gap_count", "max_delivery_gap_ms", "total_delivery_gap_ms"))
        if not previous_stop <= start <= first <= end <= stop \
                or end - first + 0.01 < response["pcm_bytes"] / 32 \
                or not 0 <= maximum <= total <= end - first + 0.01 \
                or (count == 0 and (maximum != 0 or total != 0)) \
                or (count > 0 and (maximum < 20 or total + 0.01 < count * 20 or total > count * maximum + 0.01)):
            invalid()
        previous_sequence, previous_stop = response["response_sequence"], stop
    previous_end = 0
    for caller, fixture in zip(value["caller_steps"], fixtures):
        if not isinstance(caller, dict) or set(caller) != CALLER_KEYS or caller["file_name"] != fixture.file_name \
                or caller["sha256"] != fixture.sha256 or type(caller["pcm_bytes"]) is not int \
                or not 640 <= caller["pcm_bytes"] <= 960000 or caller["pcm_bytes"] % 2 \
                or math.ceil(caller["pcm_bytes"] / 32) != fixture.duration_ms \
                or type(caller["submitted_pcm_bytes"]) is not int or caller["submitted_pcm_bytes"] % 2 \
                or not 0 < caller["submitted_pcm_bytes"] <= caller["pcm_bytes"] \
                or any(not number(caller[key]) for key in ("first_frame_ms", "last_frame_end_ms", "playback_finished_ms")) \
                or not previous_end <= caller["first_frame_ms"] <= caller["last_frame_end_ms"] <= caller["playback_finished_ms"] \
                or caller["last_frame_end_ms"] - caller["first_frame_ms"] + 0.01 < math.ceil(caller["submitted_pcm_bytes"] / 640) * 20:
            invalid()
        previous_end = caller["playback_finished_ms"]
    if value["complete"] and (not fixtures or not spans or len(value["caller_steps"]) != len(fixtures)
        or [item["response_sequence"] for item in value["responses"]] != list(spans_by_id)
        or any(item["submitted_pcm_bytes"] != item["pcm_bytes"] for item in value["caller_steps"])):
        invalid()
    return value
