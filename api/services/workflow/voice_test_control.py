"""Bounded, authenticated client for private voice admission and media storage.

This client never dials, chooses a customer endpoint, follows redirects or starts
speech providers. Only the native job service may consume a successful claim.
"""

import base64
import hashlib
import io
import json
import math
import re
import struct
import wave
from dataclasses import dataclass
from uuid import UUID

import httpx

CONTROL_URL = (
    "https://rdiwzxtibglitvtextzs.supabase.co/functions/v1/agent-test-voice-control"
)
MAX_FIXTURE_BYTES = 16000 * 2 * 30 + 4096
MAX_OUTPUT_BYTES = 16000 * 2 * 120 + 44
MEASUREMENT_SCOPE = "runtime PCM transport; excludes carrier and network latency"
HASH = re.compile(r"[0-9a-f]{64}")
STATES = {"reserved", "running", "completed", "failed", "cancelled", "expired"}


class VoiceControlError(RuntimeError):
    """A fixed code, without request bodies, provider errors or credentials."""

    def __init__(self, code="voice_control_unavailable", status_code=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def canonical_uuid(value):
    if not isinstance(value, str):
        raise VoiceControlError("voice_identity_invalid")
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise VoiceControlError("voice_identity_invalid") from None
    if parsed != value:
        raise VoiceControlError("voice_identity_invalid")
    return parsed


def positive_int(value):
    return type(value) is int and 0 < value <= 2**53 - 1


@dataclass(frozen=True)
class VoiceFixture:
    file_name: str
    sha256: str
    duration_ms: int


@dataclass(frozen=True)
class VoiceStep:
    file_name: str
    delivery: str
    delay_ms: int
    response_timeout_ms: int


@dataclass(frozen=True)
class VoiceScript:
    start_mode: str
    opening_timeout_ms: int
    steps: tuple[VoiceStep, ...]

    @classmethod
    def parse(cls, value, fixtures, duration_seconds):
        if (
            type(duration_seconds) is not int
            or not 5 <= duration_seconds <= 120
            or not isinstance(fixtures, (tuple, list))
            or not 1 <= len(fixtures) <= 10
            or any(
                not isinstance(f, VoiceFixture)
                or not isinstance(f.file_name, str)
                or not re.fullmatch(r"caller-[0-9]{3}\.wav", f.file_name)
                or not isinstance(f.sha256, str)
                or not HASH.fullmatch(f.sha256)
                or type(f.duration_ms) is not int
                or not 20 <= f.duration_ms <= 30000
                for f in fixtures
            )
            or len({f.file_name for f in fixtures}) != len(fixtures)
        ):
            raise VoiceControlError("voice_script_invalid")
        if not isinstance(value, dict) or set(value) != {
            "version",
            "start_mode",
            "opening_timeout_ms",
            "steps",
        }:
            raise VoiceControlError("voice_script_invalid")
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["start_mode"], str)
            or value["start_mode"] not in {"agent_first", "caller_first"}
        ):
            raise VoiceControlError("voice_script_invalid")
        opening = value["opening_timeout_ms"]
        if type(opening) is not int or not 500 <= opening <= 15000:
            raise VoiceControlError("voice_script_invalid")
        steps = value["steps"]
        if not isinstance(steps, list) or len(steps) != len(fixtures):
            raise VoiceControlError("voice_script_invalid")
        parsed = []
        maximum_ms = opening if value["start_mode"] == "agent_first" else 0
        for index, (step, fixture) in enumerate(zip(steps, fixtures)):
            if not isinstance(step, dict) or set(step) != {
                "file_name",
                "delivery",
                "delay_ms",
                "response_timeout_ms",
            }:
                raise VoiceControlError("voice_script_invalid")
            if (
                step["file_name"] != fixture.file_name
                or not isinstance(step["delivery"], str)
                or step["delivery"] not in {"after_response", "interrupt"}
            ):
                raise VoiceControlError("voice_script_invalid")
            if (
                index == 0
                and value["start_mode"] == "caller_first"
                and step["delivery"] == "interrupt"
            ):
                raise VoiceControlError("voice_script_invalid")
            if (
                type(step["delay_ms"]) is not int
                or not 0 <= step["delay_ms"] <= 5000
                or type(step["response_timeout_ms"]) is not int
                or not 500 <= step["response_timeout_ms"] <= 15000
            ):
                raise VoiceControlError("voice_script_invalid")
            maximum_ms += (
                fixture.duration_ms + step["delay_ms"] + step["response_timeout_ms"]
            )
            parsed.append(VoiceStep(**step))
        if maximum_ms > duration_seconds * 1000:
            raise VoiceControlError("voice_script_exceeds_budget")
        return cls(value["start_mode"], opening, tuple(parsed))


@dataclass(frozen=True)
class VoiceSession:
    id: str
    run_id: str
    workflow_id: int
    workflow_version: int
    version_kind: str
    configuration_hash: str
    runtime_workflow_hash: str
    duration_seconds: int
    scenario_id: str
    status: str
    fixtures: tuple[VoiceFixture, ...]
    script: VoiceScript | None
    runtime_workflow_run_id: int | None
    actual_duration_ms: int | None = None
    audio_sha256: str | None = None
    audio_bytes: int | None = None
    error_code: str | None = None
    provider_plan: dict | None = None

    @classmethod
    def parse(cls, value, session_id, workflow_id):
        try:
            if (
                not isinstance(value, dict)
                or value.get("id") != session_id
                or type(value.get("workflow_id")) is not int
                or value.get("workflow_id") != workflow_id
            ):
                raise ValueError
            canonical_uuid(value["id"])
            canonical_uuid(value["run_id"])
            if (
                not positive_int(value["workflow_version"])
                or value["version_kind"] not in {"draft", "published"}
                or value["status"] not in STATES
            ):
                raise ValueError
            if any(
                not isinstance(value.get(k), str) or not HASH.fullmatch(value[k])
                for k in ("configuration_hash", "runtime_workflow_hash")
            ):
                raise ValueError
            seconds = value["duration_seconds"]
            if (
                type(seconds) is not int
                or not 5 <= seconds <= 120
                or not isinstance(value["scenario_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["scenario_id"])
            ):
                raise ValueError
            manifest = value["fixture_manifest"]
            if not isinstance(manifest, list) or not 1 <= len(manifest) <= 10:
                raise ValueError
            fixtures = []
            for item in manifest:
                if not isinstance(item, dict) or set(item) != {
                    "file_name",
                    "sha256",
                    "duration_ms",
                }:
                    raise ValueError
                if (
                    not isinstance(item["file_name"], str)
                    or not re.fullmatch(r"caller-[0-9]{3}\.wav", item["file_name"])
                    or not isinstance(item["sha256"], str)
                    or not HASH.fullmatch(item["sha256"])
                ):
                    raise ValueError
                if (
                    type(item["duration_ms"]) is not int
                    or not 20 <= item["duration_ms"] <= 30000
                ):
                    raise ValueError
                fixtures.append(VoiceFixture(**item))
            if (
                len({f.file_name for f in fixtures}) != len(fixtures)
                or sum(f.duration_ms for f in fixtures) > seconds * 1000
            ):
                raise ValueError
            runtime_id = value.get("runtime_workflow_run_id")
            if runtime_id is not None and not positive_int(runtime_id):
                raise ValueError
            actual_duration = value.get("actual_duration_ms")
            audio_hash = value.get("audio_sha256")
            audio_size = value.get("audio_bytes")
            error_code = value.get("error_code")
            if actual_duration is not None and (
                type(actual_duration) is not int
                or not 0 <= actual_duration <= 2147483647
            ):
                raise ValueError
            if audio_hash is not None and (
                not isinstance(audio_hash, str) or not HASH.fullmatch(audio_hash)
            ):
                raise ValueError
            if audio_size is not None and (
                type(audio_size) is not int or not 44 <= audio_size <= MAX_OUTPUT_BYTES
            ):
                raise ValueError
            if error_code is not None and (
                not isinstance(error_code, str)
                or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code)
            ):
                raise ValueError
            script = (
                VoiceScript.parse(value["script"], fixtures, seconds)
                if value.get("script") is not None
                else None
            )
            provider_plan = value.get("provider_plan")
            if provider_plan is not None:
                from api.services.workflow.voice_test_providers import (
                    validate_private_voice_provider_plan,
                )

                provider_plan = validate_private_voice_provider_plan(provider_plan)
            return cls(
                value["id"],
                value["run_id"],
                workflow_id,
                value["workflow_version"],
                value["version_kind"],
                value["configuration_hash"],
                value["runtime_workflow_hash"],
                seconds,
                value["scenario_id"],
                value["status"],
                tuple(fixtures),
                script,
                runtime_id,
                actual_duration,
                audio_hash,
                audio_size,
                error_code,
                provider_plan,
            )
        except (KeyError, TypeError, ValueError):
            raise VoiceControlError("voice_control_protocol_invalid") from None


def output_measurements(encoded: bytes, duration_seconds: int) -> dict:
    if (
        type(duration_seconds) is not int
        or not 5 <= duration_seconds <= 120
        or not isinstance(encoded, bytes)
        or not 44 <= len(encoded) <= MAX_OUTPUT_BYTES
        or encoded[:4] != b"RIFF"
        or encoded[8:16] != b"WAVEfmt "
        or encoded[36:40] != b"data"
    ):
        raise VoiceControlError("voice_output_invalid")
    if (
        struct.unpack_from("<I", encoded, 4)[0] != len(encoded) - 8
        or struct.unpack_from("<I", encoded, 16)[0] != 16
        or struct.unpack_from("<I", encoded, 40)[0] != len(encoded) - 44
    ):
        raise VoiceControlError("voice_output_invalid")
    if struct.unpack_from("<HHIIHH", encoded, 20) != (1, 1, 16000, 32000, 2, 16):
        raise VoiceControlError("voice_output_invalid")
    pcm = encoded[44:]
    if len(pcm) % 2 or len(pcm) > duration_seconds * 32000:
        raise VoiceControlError("voice_output_invalid")
    return {
        "sample_rate": 16000,
        "channels": 1,
        "pcm_bytes": len(pcm),
        "duration_seconds": len(pcm) / 32000,
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        "measurement_scope": MEASUREMENT_SCOPE,
    }


class VoiceControlClient:
    def __init__(self, client: httpx.AsyncClient, secret: str):
        if not isinstance(secret, str) or not secret:
            raise VoiceControlError("voice_control_not_configured")
        self._client = client
        self._secret = secret

    def __repr__(self):
        return "VoiceControlClient()"

    async def _exchange(self, body, maximum=65536):
        try:
            async with self._client.stream(
                "POST",
                CONTROL_URL,
                json=body,
                headers={
                    "x-sysevo-secret": self._secret,
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
            ) as response:
                if response.status_code != 200:
                    raise VoiceControlError(
                        "voice_control_rejected", response.status_code
                    )
                declared = response.headers.get("content-length")
                if declared is not None and (
                    not declared.isdigit() or int(declared) > maximum
                ):
                    raise VoiceControlError("voice_control_response_too_large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > maximum:
                        raise VoiceControlError("voice_control_response_too_large")
                    data.extend(chunk)
                return bytes(data), response.headers
        except (httpx.HTTPError, TimeoutError):
            raise VoiceControlError("voice_control_unavailable") from None

    async def _json(self, body, expected_status=None, maximum=65536):
        encoded, _ = await self._exchange(body, maximum)
        try:
            value = json.loads(encoded)
            if not isinstance(value, dict) or (
                expected_status is not None and value.get("status") != expected_status
            ):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise VoiceControlError("voice_control_protocol_invalid") from None

    @staticmethod
    def _identity(operation, session_id, lease_token, workflow_id):
        canonical_uuid(session_id)
        canonical_uuid(lease_token)
        if not positive_int(workflow_id):
            raise VoiceControlError("voice_identity_invalid")
        return {
            "operation": operation,
            "session_id": session_id,
            "lease_token": lease_token,
            "workflow_id": workflow_id,
        }

    async def get(self, session_id, lease_token, workflow_id):
        value = await self._json(
            self._identity("get", session_id, lease_token, workflow_id), maximum=131072
        )
        return VoiceSession.parse(value.get("session"), session_id, workflow_id)

    async def claim(self, session, lease_token, claim_token):
        canonical_uuid(claim_token)
        body = self._identity("claim", session.id, lease_token, session.workflow_id)
        await self._json(
            {
                **body,
                "claim_token": claim_token,
                "workflow_version": session.workflow_version,
                "runtime_workflow_hash": session.runtime_workflow_hash,
            },
            "claimed",
        )

    async def providers(self, session, lease_token):
        """Read only the profiles pinned in this active session's immutable plan."""
        from api.services.workflow.voice_test_providers import (
            TestProviderConnection,
            validate_private_voice_provider_plan,
        )

        plan = validate_private_voice_provider_plan(session.provider_plan)
        body = self._identity(
            "get_providers", session.id, lease_token, session.workflow_id
        )
        encoded, _ = await self._exchange(body, maximum=196608)
        try:
            value = json.loads(encoded)
            if (
                not isinstance(value, dict)
                or set(value) != {"status", "session_id", "workflow_id", "providers"}
                or value["status"] != "providers_ready"
                or value["session_id"] != session.id
                or type(value["workflow_id"]) is not int
                or value["workflow_id"] != session.workflow_id
                or not isinstance(value["providers"], list)
                or len(value["providers"]) != len(plan["bindings"])
            ):
                raise VoiceControlError("voice_test_provider_response_invalid")
            seen = set()
            for packet in value["providers"]:
                connection = TestProviderConnection.parse(packet, self._secret)
                expected = plan["bindings"].get(connection.service_type)
                if (
                    connection.service_type in seen
                    or expected
                    != {
                        "id": connection.id,
                        "revision": connection.revision,
                        "fingerprint": connection.fingerprint,
                    }
                    or plan["services"][connection.service_type]["provider"]
                    != connection.provider
                ):
                    raise VoiceControlError("voice_test_provider_binding_invalid")
                seen.add(connection.service_type)
            if seen != set(plan["bindings"]):
                raise VoiceControlError("voice_test_provider_binding_invalid")
            return value["providers"]
        except VoiceControlError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise VoiceControlError("voice_test_provider_response_invalid") from None

    async def attach(self, session, lease_token, claim_token, runtime_id):
        canonical_uuid(claim_token)
        if not positive_int(runtime_id):
            raise VoiceControlError("voice_identity_invalid")
        body = self._identity("attach", session.id, lease_token, session.workflow_id)
        await self._json(
            {**body, "claim_token": claim_token, "runtime_workflow_run_id": runtime_id},
            "runtime_attached",
        )

    async def fixture(self, session, lease_token, fixture):
        if fixture not in session.fixtures:
            raise VoiceControlError("voice_fixture_not_planned")
        body = self._identity(
            "get_fixture", session.id, lease_token, session.workflow_id
        )
        encoded, headers = await self._exchange(
            {**body, "file_name": fixture.file_name}, MAX_FIXTURE_BYTES
        )
        if (
            hashlib.sha256(encoded).hexdigest() != fixture.sha256
            or headers.get("x-sysevo-audio-sha256") != fixture.sha256
            or headers.get("content-type", "").split(";")[0]
            not in {"audio/wav", "audio/x-wav"}
        ):
            raise VoiceControlError("voice_fixture_hash_invalid")
        try:
            with wave.open(io.BytesIO(encoded), "rb") as stream:
                frames = stream.getnframes()
                if (
                    (
                        stream.getnchannels(),
                        stream.getsampwidth(),
                        stream.getframerate(),
                        stream.getcomptype(),
                    )
                    != (1, 2, 16000, "NONE")
                    or not 320 <= frames <= 480000
                    or math.ceil(frames / 16) != fixture.duration_ms
                    or len(stream.readframes(frames)) != frames * 2
                ):
                    raise ValueError
        except (wave.Error, EOFError, ValueError):
            raise VoiceControlError("voice_fixture_invalid") from None
        return encoded

    async def upload(self, session, lease_token, claim_token, encoded):
        canonical_uuid(claim_token)
        metrics = output_measurements(encoded, session.duration_seconds)
        body = self._identity("put_audio", session.id, lease_token, session.workflow_id)
        value = await self._json(
            {
                **body,
                "claim_token": claim_token,
                "audio_base64": base64.b64encode(encoded).decode("ascii"),
            },
            "audio_verified",
        )
        received = value.get("safe_metrics")
        typed_metrics = (
            isinstance(received, dict)
            and all(
                type(received.get(key)) is int
                for key in ("sample_rate", "channels", "pcm_bytes")
            )
            and type(received.get("duration_seconds")) in (int, float)
        )
        if (
            value.get("audio_sha256") != hashlib.sha256(encoded).hexdigest()
            or type(value.get("audio_bytes")) is not int
            or value.get("audio_bytes") != len(encoded)
            or not typed_metrics
            or received != metrics
        ):
            raise VoiceControlError("voice_output_readback_invalid")
        return metrics

    async def finish(
        self,
        session,
        lease_token,
        claim_token,
        *,
        status,
        duration_ms,
        error_code=None,
        retain_audio=False,
    ):
        canonical_uuid(claim_token)
        if (
            not isinstance(status, str)
            or status not in {"completed", "failed"}
            or (
                duration_ms is not None
                and (type(duration_ms) is not int or not 0 <= duration_ms <= 2147483647)
            )
            or (
                error_code is not None
                and (
                    not isinstance(error_code, str)
                    or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code)
                )
            )
            or type(retain_audio) is not bool
        ):
            raise VoiceControlError("voice_result_invalid")
        if status == "completed" and (duration_ms is None or error_code is not None):
            raise VoiceControlError("voice_result_invalid")
        body = self._identity("finish", session.id, lease_token, session.workflow_id)
        await self._json(
            {
                **body,
                "claim_token": claim_token,
                "status": status,
                "actual_duration_ms": duration_ms,
                "error_code": error_code,
                "retain_audio": retain_audio,
            },
            "settled",
        )
