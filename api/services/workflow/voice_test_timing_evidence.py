"""Bounded native PCM submission/playback times, with no audio or text contents."""
import math

from api.services.workflow.voice_test_control import VoiceControlError

TIMING_SCOPE = "native monotonic PCM delivery; excludes phonetic pauses, carrier and network latency"
TIMING_SCOPE_V2 = "native monotonic PCM submission and interruption events; nominal packet end includes cancelled pacing; excludes phonetic pauses, carrier and network latency"
TIMING_SCOPE_V3 = "native monotonic PCM submission, interruption events and sampled callback mute policy; other mute strategies are not observed; excludes phonetic pauses, carrier and network latency"
TIMING_SCOPE_V4 = "native monotonic PCM submission, interruption events and sampled outputs of the three configured mute strategies; excludes provider-owned turn detection, phonetic pauses, carrier and network latency"
TIMING_SCOPE_V5 = "native monotonic PCM submission, interruption events, three mute-strategy outputs and configured turn-start source; excludes provider turn-detection internals, phonetic pauses, carrier and network latency"
TIMING_SCOPE_V6 = "native monotonic PCM submission, interruption events, three mute-strategy outputs, per-callback node interrupt policy and configured turn-start source; excludes provider turn-detection internals, phonetic pauses, carrier and network latency"
TURN_DETECTION_SOURCES = {
    "google_server_vad", "realtime_external_events", "realtime_local_vad",
    "deepgram_flux_vad_external", "vad_transcription_turn_analyzer", "vad_transcription_speech_timeout",
}
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
        self.interruptions = []
        self.cancelled = {}
        self.mute_policy = []
        self.last_mute_policy = {}
        self.turn_detection_source = None
        self.turn_signals = []

    def set_turn_detection_source(self, source):
        if type(source) is not str or source not in TURN_DETECTION_SOURCES:
            self.invalid = True
            return
        self.turn_detection_source = source

    def record_turn_start(self, at, signal):
        self.observe(at)
        caller = self.callers[-1] if self.callers else None
        if caller is None or caller["first"] is None or caller["finished"] is not None:
            return
        if type(signal) is not str or signal not in {"user_turn_started", "vad_user_started"} or len(self.turn_signals) >= 128:
            self.invalid = True
            return
        self.turn_signals.append(dict(at=at, caller_step=len(self.callers), signal=signal))

    def observe(self, at):
        if type(at) not in (int, float) or not math.isfinite(at):
            self.invalid = True
            return
        if self.origin is None:
            self.origin = at
        if at < self.origin:
            self.invalid = True

    def record_output(self, at, size, sequence, *, pcm_start_byte=None):
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
        item["last_write"] = dict(pcm_start_byte=pcm_start_byte, size=size, at=at)

    def record_cancelled_output(self, at, sequence, pcm_start_byte, size):
        self.observe(at)
        audio = self.audio.get(sequence)
        last = audio.get("last_write") if audio else None
        if sequence in self.cancelled or last is None or type(pcm_start_byte) is not int \
                or last["pcm_start_byte"] != pcm_start_byte or last["size"] != size or at < last["at"]:
            self.invalid = True
            return
        self.cancelled[sequence] = dict(pcm_start_byte=pcm_start_byte, pcm_end_byte=pcm_start_byte + size,
                                       submitted_at=last["at"], cancelled_at=at)

    def record_interruption(self, at, sequence):
        self.observe(at)
        if len(self.interruptions) >= 128:
            self.invalid = True
            return
        caller = self.callers[-1] if self.callers else None
        self.interruptions.append(dict(at=at, response_sequence=sequence, caller_step=len(self.callers),
            caller_active=caller is not None and caller["first"] is not None and caller["finished"] is None))

    def record_mute_policy(self, at, state):
        """Record changes in the node/queued-speech mute callback during a caller turn."""
        self.observe(at)
        caller = self.callers[-1] if self.callers else None
        if caller is None or caller["first"] is None or caller["finished"] is not None:
            return
        muted = state not in {"unmuted", "self_heal_release"}
        self.record_strategy_mute(at, "workflow_callback", muted, state)

    def record_strategy_mute(self, at, strategy, muted, reason=None, node_allows_interrupt=None):
        self.observe(at)
        caller = self.callers[-1] if self.callers else None
        if caller is None or caller["first"] is None or caller["finished"] is not None:
            return
        sources = {"first_bot_complete", "function_call", "workflow_callback"}
        callback_reasons = {"pipeline_shutdown", "queued_speech", "node_forbids_interruption", "unmuted", "self_heal_release"}
        if strategy not in sources or type(muted) is not bool \
                or (strategy == "workflow_callback" and reason not in callback_reasons) \
                or (strategy != "workflow_callback" and reason is not None) \
                or (strategy == "workflow_callback" and node_allows_interrupt is not None and type(node_allows_interrupt) is not bool) \
                or (strategy != "workflow_callback" and node_allows_interrupt is not None) \
                or (strategy == "workflow_callback" and muted != (reason not in {"unmuted", "self_heal_release"})):
            self.invalid = True
            return
        step = len(self.callers)
        key = (step, strategy)
        state = (muted, reason, node_allows_interrupt)
        if self.last_mute_policy.get(key) == state:
            return
        if len(self.mute_policy) >= 384:
            self.invalid = True
            return
        self.last_mute_policy[key] = state
        self.mute_policy.append(dict(at=at, caller_step=step, strategy=strategy, muted=muted, reason=reason,
                                     node_allows_interrupt=node_allows_interrupt))

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
        v2 = bool(self.interruptions or self.cancelled)
        v4 = bool(self.callers)
        v5 = bool(self.callers)
        v6 = bool(self.callers)
        result = dict(version=6 if v6 else 1 if not v2 else 2,
                      scope=TIMING_SCOPE_V6 if v6 else TIMING_SCOPE_V2 if v2 else TIMING_SCOPE,
                      complete=False, caller_steps=[], responses=[])
        if v2 or v4:
            result["interruptions"] = []
        if v4:
            result["mute_policy"] = {"strategies": ["first_bot_complete", "function_call", "workflow_callback"], "samples": []}
        if v5:
            result["turn_detection"] = {"source": self.turn_detection_source, "signals": []}
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
            if v2 or v4:
                cancelled = self.cancelled.get(response.sequence)
                result["responses"][-1]["cancelled_write"] = None if cancelled is None else dict(
                    pcm_start_byte=cancelled["pcm_start_byte"], pcm_end_byte=cancelled["pcm_end_byte"],
                    submitted_ms=relative(cancelled["submitted_at"]), cancelled_ms=relative(cancelled["cancelled_at"]))
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
        if v2 or v4:
            result["interruptions"] = [dict(sequence=index + 1, at_ms=relative(item["at"]),
                response_sequence=item["response_sequence"], caller_step=item["caller_step"], caller_active=item["caller_active"])
                for index, item in enumerate(self.interruptions)]
        if v4:
            result["mute_policy"]["samples"] = [dict(sequence=index + 1, at_ms=relative(item["at"]),
                caller_step=item["caller_step"], strategy=item["strategy"], muted=item["muted"], reason=item["reason"],
                node_allows_interrupt=item["node_allows_interrupt"])
                for index, item in enumerate(self.mute_policy)]
        if v5:
            result["turn_detection"]["signals"] = [dict(sequence=index + 1, at_ms=relative(item["at"]),
                caller_step=item["caller_step"], signal=item["signal"])
                for index, item in enumerate(self.turn_signals)]
        result["complete"] = bool(pipeline_stopped is True and valid and self.responses and self.callers
                                  and len(self.audio) == len(self.responses)
                                  and (not v4 or {(item["caller_step"], item["strategy"]) for item in self.mute_policy}
                                       >= {(step, strategy) for step in range(1, len(self.callers) + 1)
                                           for strategy in ("first_bot_complete", "function_call", "workflow_callback")})
                                  and (not v5 or self.turn_detection_source in TURN_DETECTION_SOURCES))
        return result


def validate_voice_timing_evidence(value, *, duration_ms, fixtures, spans):
    def invalid():
        raise VoiceControlError("voice_timing_evidence_invalid")

    def number(item):
        return type(item) in (int, float) and math.isfinite(item) and 0 <= item <= duration_ms

    version = value.get("version") if isinstance(value, dict) else None
    v2 = type(version) is int and version in (2, 3, 4, 5, 6)
    v3 = type(version) is int and version == 3
    v4 = type(version) is int and version in (4, 5, 6)
    v5 = type(version) is int and version in (5, 6)
    v6 = type(version) is int and version == 6
    keys = {"version", "scope", "complete", "caller_steps", "responses"} | ({"interruptions"} if v2 else set()) | ({"callback_mute_policy"} if v3 else set()) | ({"mute_policy"} if v4 else set()) | ({"turn_detection"} if v5 else set())
    if not isinstance(value, dict) or set(value) != keys \
            or type(value["version"]) is not int or value["version"] not in (1, 2, 3, 4, 5, 6) \
            or value["scope"] != (TIMING_SCOPE_V6 if v6 else TIMING_SCOPE_V5 if v5 else TIMING_SCOPE_V4 if v4 else TIMING_SCOPE_V3 if v3 else TIMING_SCOPE_V2 if v2 else TIMING_SCOPE) \
            or type(value["complete"]) is not bool or not isinstance(value["caller_steps"], list) \
            or len(value["caller_steps"]) > len(fixtures) or not isinstance(value["responses"], list) \
            or len(value["responses"]) > 128:
        invalid()
    spans_by_id = {item["response_sequence"]: item["pcm_end_byte"] - item["pcm_start_byte"] for item in spans}
    previous_sequence, previous_stop = 0, 0
    for response in value["responses"]:
        if not isinstance(response, dict) or set(response) != RESPONSE_KEYS | ({"cancelled_write"} if v2 else set()) \
                or type(response["response_sequence"]) is not int or not previous_sequence < response["response_sequence"] <= 128 \
                or type(response["pcm_bytes"]) is not int or response["pcm_bytes"] != spans_by_id.get(response["response_sequence"]) \
                or type(response["delivery_gap_count"]) is not int or not 0 <= response["delivery_gap_count"] <= response["pcm_bytes"] / 2 \
                or any(not number(response[key]) for key in ("started_ms", "first_pcm_ms", "stopped_ms", "max_delivery_gap_ms", "total_delivery_gap_ms")):
            invalid()
        start, first, end, stop = (response[key] for key in ("started_ms", "first_pcm_ms", "last_pcm_end_ms", "stopped_ms"))
        count, maximum, total = (response[key] for key in ("delivery_gap_count", "max_delivery_gap_ms", "total_delivery_gap_ms"))
        cancelled = response.get("cancelled_write") if v2 else None
        if not number(end) and cancelled is None:
            invalid()
        if cancelled is not None:
            span = next((item for item in spans if item["response_sequence"] == response["response_sequence"]), None)
            if not isinstance(cancelled, dict) or set(cancelled) != {"pcm_start_byte", "pcm_end_byte", "submitted_ms", "cancelled_ms"} \
                    or any(type(cancelled[key]) is not int for key in ("pcm_start_byte", "pcm_end_byte")) \
                    or span is None or not span["pcm_start_byte"] <= cancelled["pcm_start_byte"] < cancelled["pcm_end_byte"] == span["pcm_end_byte"] \
                    or cancelled["pcm_start_byte"] % 2 or cancelled["pcm_end_byte"] % 2 \
                    or not number(cancelled["submitted_ms"]) or not number(cancelled["cancelled_ms"]) \
                    or not first <= cancelled["submitted_ms"] <= cancelled["cancelled_ms"] <= stop \
                    or type(end) not in (int, float) or not math.isfinite(end) \
                    or abs(end - cancelled["submitted_ms"] - (cancelled["pcm_end_byte"] - cancelled["pcm_start_byte"]) / 32) > 0.01:
                invalid()
        if not previous_stop <= start <= first <= end or (cancelled is None and end > stop) \
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
    if v2:
        if not isinstance(value["interruptions"], list) or len(value["interruptions"]) > 128:
            invalid()
        previous_at = 0
        responses_by_id = {item["response_sequence"]: item for item in value["responses"]}
        for index, event in enumerate(value["interruptions"]):
            if not isinstance(event, dict) or set(event) != {"sequence", "at_ms", "response_sequence", "caller_step", "caller_active"} \
                    or type(event["sequence"]) is not int or event["sequence"] != index + 1 \
                    or not number(event["at_ms"]) or event["at_ms"] < previous_at \
                    or type(event["caller_step"]) is not int or not 0 <= event["caller_step"] <= len(fixtures) \
                    or type(event["caller_active"]) is not bool:
                invalid()
            sequence = event["response_sequence"]
            if sequence is not None:
                response = responses_by_id.get(sequence) if type(sequence) is int else None
                if response is None or not response["started_ms"] <= event["at_ms"] <= response["stopped_ms"]:
                    invalid()
            step = event["caller_step"]
            callers = value["caller_steps"]
            previous = callers[step - 2] if 2 <= step <= len(callers) + 1 else None
            following = callers[step] if step < len(callers) else None
            current = callers[step - 1] if 1 <= step <= len(callers) else None
            if (previous and event["at_ms"] < previous["playback_finished_ms"]) \
                    or (following and event["at_ms"] > following["first_frame_ms"]) \
                    or (not event["caller_active"] and current and current["first_frame_ms"] < event["at_ms"] < current["playback_finished_ms"]):
                invalid()
            if event["caller_active"]:
                if step < 1 or step > len(value["caller_steps"]):
                    invalid()
                caller = value["caller_steps"][step - 1]
                if not caller["first_frame_ms"] <= event["at_ms"] <= caller["playback_finished_ms"]:
                    invalid()
            previous_at = event["at_ms"]
    if v3:
        policy = value["callback_mute_policy"]
        if not isinstance(policy, dict) or set(policy) != {"other_mute_strategies_observed", "samples"} \
                or policy["other_mute_strategies_observed"] is not False or not isinstance(policy["samples"], list) \
                or len(policy["samples"]) > 128:
            invalid()
        previous_at = 0
        last_state = {}
        sampled_steps = set()
        for index, sample in enumerate(policy["samples"]):
            if not isinstance(sample, dict) or set(sample) != {"sequence", "at_ms", "caller_step", "state"} \
                    or type(sample["sequence"]) is not int or sample["sequence"] != index + 1 \
                    or not number(sample["at_ms"]) or sample["at_ms"] < previous_at \
                    or type(sample["caller_step"]) is not int or not 1 <= sample["caller_step"] <= len(value["caller_steps"]) \
                    or type(sample["state"]) is not str \
                    or sample["state"] not in {"pipeline_shutdown", "queued_speech", "node_forbids_interruption", "unmuted", "self_heal_release"} \
                    or last_state.get(sample["caller_step"]) == sample["state"]:
                invalid()
            caller = value["caller_steps"][sample["caller_step"] - 1]
            if not caller["first_frame_ms"] <= sample["at_ms"] <= caller["playback_finished_ms"]:
                invalid()
            last_state[sample["caller_step"]] = sample["state"]
            sampled_steps.add(sample["caller_step"])
            previous_at = sample["at_ms"]
        if value["complete"] and sampled_steps != set(range(1, len(value["caller_steps"]) + 1)):
            invalid()
    if v4:
        policy = value["mute_policy"]
        strategies = ["first_bot_complete", "function_call", "workflow_callback"]
        if not isinstance(policy, dict) or set(policy) != {"strategies", "samples"} \
                or policy["strategies"] != strategies or not isinstance(policy["samples"], list) \
                or len(policy["samples"]) > 384:
            invalid()
        previous_at = 0
        last_states = {}
        sampled = set()
        for index, sample in enumerate(policy["samples"]):
            if not isinstance(sample, dict) or set(sample) != {"sequence", "at_ms", "caller_step", "strategy", "muted", "reason"} | ({"node_allows_interrupt"} if v6 else set()) \
                    or type(sample["sequence"]) is not int or sample["sequence"] != index + 1 \
                    or not number(sample["at_ms"]) or sample["at_ms"] < previous_at \
                    or type(sample["caller_step"]) is not int or not 1 <= sample["caller_step"] <= len(value["caller_steps"]) \
                    or type(sample["strategy"]) is not str or sample["strategy"] not in strategies \
                    or type(sample["muted"]) is not bool:
                invalid()
            if sample["strategy"] == "workflow_callback":
                if type(sample["reason"]) is not str or sample["reason"] not in {
                    "pipeline_shutdown", "queued_speech", "node_forbids_interruption", "unmuted", "self_heal_release"
                } or sample["muted"] != (sample["reason"] not in {"unmuted", "self_heal_release"}):
                    invalid()
                if v6 and sample["node_allows_interrupt"] is not None and type(sample["node_allows_interrupt"]) is not bool:
                    invalid()
            elif sample["reason"] is not None:
                invalid()
            elif v6 and sample["node_allows_interrupt"] is not None:
                invalid()
            key = (sample["caller_step"], sample["strategy"])
            state = (sample["muted"], sample["reason"], sample.get("node_allows_interrupt"))
            if last_states.get(key) == state:
                invalid()
            caller = value["caller_steps"][sample["caller_step"] - 1]
            if not caller["first_frame_ms"] <= sample["at_ms"] <= caller["playback_finished_ms"]:
                invalid()
            last_states[key] = state
            sampled.add(key)
            previous_at = sample["at_ms"]
        required = {(step, strategy) for step in range(1, len(value["caller_steps"]) + 1) for strategy in strategies}
        if value["complete"] and sampled != required:
            invalid()
    if v5:
        turn = value["turn_detection"]
        if not isinstance(turn, dict) or set(turn) != {"source", "signals"} \
                or (turn["source"] is not None and (type(turn["source"]) is not str or turn["source"] not in TURN_DETECTION_SOURCES)) \
                or not isinstance(turn["signals"], list) or len(turn["signals"]) > 128:
            invalid()
        previous_at = 0
        for index, signal in enumerate(turn["signals"]):
            if not isinstance(signal, dict) or set(signal) != {"sequence", "at_ms", "caller_step", "signal"} \
                    or type(signal["sequence"]) is not int or signal["sequence"] != index + 1 \
                    or not number(signal["at_ms"]) or signal["at_ms"] < previous_at \
                    or type(signal["caller_step"]) is not int or not 1 <= signal["caller_step"] <= len(value["caller_steps"]) \
                    or type(signal["signal"]) is not str or signal["signal"] not in {"user_turn_started", "vad_user_started"}:
                invalid()
            caller = value["caller_steps"][signal["caller_step"] - 1]
            if not caller["first_frame_ms"] <= signal["at_ms"] <= caller["playback_finished_ms"]:
                invalid()
            previous_at = signal["at_ms"]
        if value["complete"] and turn["source"] not in TURN_DETECTION_SOURCES:
            invalid()
    return value
