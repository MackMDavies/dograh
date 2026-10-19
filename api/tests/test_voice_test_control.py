import copy
import hashlib
import io
import json
import wave

import httpx
import pytest

from api.services.workflow.voice_test_control import (
    CONTROL_URL,
    MAX_FIXTURE_BYTES,
    MAX_OUTPUT_BYTES,
    VoiceControlClient,
    VoiceControlError,
    VoiceFixture,
    VoiceScript,
    VoiceSession,
    output_measurements,
)

SESSION = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
LEASE = "33333333-3333-4333-8333-333333333333"
CLAIM = "44444444-4444-4444-8444-444444444444"
SECRET = "synthetic-voice-control-secret"


def wav_bytes(seconds=1):
    value = io.BytesIO()
    with wave.open(value, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(bytes(int(seconds * 32000)))
    return value.getvalue()


@pytest.mark.parametrize("seconds", [0, 4, 121, True, "30", float("nan")])
def test_capture_measurements_require_a_real_reserved_duration(seconds):
    with pytest.raises(VoiceControlError, match="voice_output_invalid"):
        output_measurements(wav_bytes(), seconds)


def metadata():
    return {
        "id": SESSION,
        "run_id": RUN,
        "workflow_id": 185,
        "workflow_version": 3,
        "version_kind": "published",
        "configuration_hash": "a" * 64,
        "runtime_workflow_hash": "b" * 64,
        "duration_seconds": 30,
        "scenario_id": "voice_0",
        "status": "reserved",
        "runtime_workflow_run_id": None,
        "fixture_manifest": [
            {
                "file_name": "caller-001.wav",
                "sha256": hashlib.sha256(wav_bytes()).hexdigest(),
                "duration_ms": 1000,
            }
        ],
        "script": {
            "version": 1,
            "start_mode": "agent_first",
            "opening_timeout_ms": 5000,
            "steps": [
                {
                    "file_name": "caller-001.wav",
                    "delivery": "after_response",
                    "delay_ms": 0,
                    "response_timeout_ms": 5000,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_bound_metadata_and_nonsecret_repr():
    def handle(request):
        assert str(request.url) == CONTROL_URL
        assert request.headers["x-sysevo-secret"] == SECRET
        assert request.headers["accept-encoding"] == "identity"
        assert json.loads(request.content) == {
            "operation": "get",
            "session_id": SESSION,
            "lease_token": LEASE,
            "workflow_id": 185,
        }
        return httpx.Response(200, json={"session": metadata()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = VoiceControlClient(http, SECRET)
        session = await client.get(SESSION, LEASE, 185)
        assert (
            session.run_id == RUN
            and session.script.steps[0].file_name == "caller-001.wav"
        )
        assert SECRET not in repr(client)


@pytest.mark.parametrize(
    "mutation",
    [
        {"workflow_id": 186},
        {"workflow_id": True},
        {"id": RUN},
        {"workflow_version": True},
        {"version_kind": "latest"},
        {"duration_seconds": 121},
        {"duration_seconds": True},
        {"status": "passed"},
        {"configuration_hash": "missing"},
        {"run_id": "../bad"},
        {"scenario_id": "../../client"},
        {"fixture_manifest": []},
        {"runtime_workflow_run_id": 0},
        {"actual_duration_ms": True},
        {"actual_duration_ms": -1},
        {"audio_sha256": "not-a-hash"},
        {"audio_bytes": True},
        {"audio_bytes": 1},
        {"error_code": "private provider body"},
    ],
)
def test_rejects_invalid_job_metadata(mutation):
    with pytest.raises(VoiceControlError):
        VoiceSession.parse({**metadata(), **mutation}, SESSION, 185)


def test_does_not_invent_a_script_for_old_controller_metadata():
    data = metadata()
    data.pop("script")
    assert VoiceSession.parse(data, SESSION, 185).script is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(start_mode=[]),
        lambda s: s.update(version=True),
        lambda s: s.update(opening_timeout_ms=0),
        lambda s: s.update(steps=[]),
        lambda s: s["steps"][0].update(file_name="caller-002.wav"),
        lambda s: s["steps"][0].update(delivery=[]),
        lambda s: s["steps"][0].update(delay_ms=-1),
        lambda s: s["steps"][0].update(response_timeout_ms=True),
        lambda s: s.update(destination="client"),
    ],
)
def test_script_is_explicit_and_bounded(mutate):
    session = VoiceSession.parse(metadata(), SESSION, 185)
    script = copy.deepcopy(metadata()["script"])
    mutate(script)
    with pytest.raises(VoiceControlError):
        VoiceScript.parse(script, session.fixtures, 30)


def test_script_requires_enough_worst_case_time_and_cannot_interrupt_before_caller_first():
    session = VoiceSession.parse(metadata(), SESSION, 185)
    with pytest.raises(VoiceControlError, match="voice_script_exceeds_budget"):
        VoiceScript.parse(metadata()["script"], session.fixtures, 5)
    script = metadata()["script"]
    script["start_mode"] = "caller_first"
    script["steps"][0]["delivery"] = "interrupt"
    with pytest.raises(VoiceControlError):
        VoiceScript.parse(script, session.fixtures, 30)


@pytest.mark.parametrize(
    "fixtures,seconds",
    [
        ([], 30),
        ([None], 30),
        ([VoiceFixture("caller-001.wav", "a" * 64, 0)], 30),
        ([VoiceFixture("caller-001.wav", "a" * 64, 1000)] * 2, 30),
        ([VoiceFixture("caller-001.wav", "a" * 64, 1000)], True),
    ],
)
def test_script_parser_independently_rejects_invalid_manifests(fixtures, seconds):
    with pytest.raises(VoiceControlError):
        VoiceScript.parse(metadata()["script"], fixtures, seconds)


@pytest.mark.asyncio
async def test_never_follows_redirects_or_exposes_error_bodies():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://customer.invalid/exfiltrate"},
            text=SECRET,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        with pytest.raises(VoiceControlError) as error:
            await VoiceControlClient(http, SECRET).get(SESSION, LEASE, 185)
        assert error.value.status_code == 307 and SECRET not in str(error.value)
    assert len(requests) == 1 and str(requests[0].url) == CONTROL_URL


@pytest.mark.asyncio
async def test_transport_errors_are_fixed_codes():
    def handle(request):
        raise httpx.ReadTimeout(SECRET, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(
            VoiceControlError, match="^voice_control_unavailable$"
        ) as error:
            await VoiceControlClient(http, SECRET).get(SESSION, LEASE, 185)
        assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, headers={"content-length": "999999999"}, json={}),
        httpx.Response(200, content=b"x" * 65537),
    ],
)
async def test_metadata_response_size_and_shape_are_bounded(response):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http:
        with pytest.raises(VoiceControlError):
            await VoiceControlClient(http, SECRET).get(SESSION, LEASE, 185)


@pytest.mark.asyncio
async def test_fixture_is_planned_hashed_and_parsed_independently():
    session = VoiceSession.parse(metadata(), SESSION, 185)
    fixture = session.fixtures[0]

    def handle(request):
        assert json.loads(request.content)["file_name"] == fixture.file_name
        return httpx.Response(
            200,
            content=wav_bytes(),
            headers={
                "content-type": "audio/wav",
                "x-sysevo-audio-sha256": fixture.sha256,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert (
            await VoiceControlClient(http, SECRET).fixture(session, LEASE, fixture)
            == wav_bytes()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hash", "header", "mime", "too_big"])
async def test_fixture_readback_rejects_changed_bytes_or_headers(bad):
    session = VoiceSession.parse(metadata(), SESSION, 185)
    fixture = session.fixtures[0]
    data = wav_bytes()
    headers = {"content-type": "audio/wav", "x-sysevo-audio-sha256": fixture.sha256}
    if bad == "hash":
        data = data[:-1] + b"x"
    elif bad == "header":
        headers["x-sysevo-audio-sha256"] = "e" * 64
    elif bad == "mime":
        headers["content-type"] = "text/plain"
    elif bad == "too_big":
        data = bytes(MAX_FIXTURE_BYTES + 1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=data, headers=headers)
        )
    ) as http:
        with pytest.raises(VoiceControlError):
            await VoiceControlClient(http, SECRET).fixture(session, LEASE, fixture)


@pytest.mark.asyncio
async def test_claim_attach_and_settlement_do_not_retry_or_invent_success():
    session = VoiceSession.parse(metadata(), SESSION, 185)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        status = {
            "claim": "claimed",
            "attach": "runtime_attached",
            "finish": "settled",
        }[body["operation"]]
        return httpx.Response(200, json={"status": status})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = VoiceControlClient(http, SECRET)
        await client.claim(session, LEASE, CLAIM)
        await client.attach(session, LEASE, CLAIM, 123)
        await client.finish(
            session,
            LEASE,
            CLAIM,
            status="failed",
            duration_ms=None,
            error_code="voice_pipeline_failed",
        )
    assert len(requests) == 3
    assert requests[0]["runtime_workflow_hash"] == session.runtime_workflow_hash
    assert requests[1]["runtime_workflow_run_id"] == 123
    assert requests[2]["actual_duration_ms"] is None and not requests[2]["retain_audio"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "sha", "metrics", "boolean", "bytes"])
async def test_output_upload_checks_independent_readback_measurements(corrupt):
    session = VoiceSession.parse(metadata(), SESSION, 185)
    data = wav_bytes()
    metrics = output_measurements(data, 30)
    value = {
        "status": "audio_verified",
        "audio_sha256": hashlib.sha256(data).hexdigest(),
        "audio_bytes": len(data),
        "safe_metrics": metrics,
    }
    if corrupt == "sha":
        value["audio_sha256"] = "f" * 64
    elif corrupt == "metrics":
        value["safe_metrics"] = {**metrics, "transcript": "private"}
    elif corrupt == "boolean":
        value["safe_metrics"] = {**metrics, "channels": True}
    elif corrupt == "bytes":
        value["audio_bytes"] = 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))
    ) as http:
        client = VoiceControlClient(http, SECRET)
        if corrupt:
            with pytest.raises(
                VoiceControlError, match="voice_output_readback_invalid"
            ):
                await client.upload(session, LEASE, CLAIM, data)
        else:
            assert await client.upload(session, LEASE, CLAIM, data) == metrics


def test_maximum_native_output_and_empty_output_have_honest_capture_measurements():
    data = wav_bytes(120)
    assert len(data) == MAX_OUTPUT_BYTES
    assert output_measurements(data, 120)["pcm_bytes"] == 3840000
    assert output_measurements(wav_bytes(0), 30)["pcm_bytes"] == 0
    with pytest.raises(VoiceControlError):
        output_measurements(data, 119)
    with pytest.raises(VoiceControlError):
        output_measurements(data[:-2], 120)


@pytest.mark.asyncio
async def test_signed_test_profiles_are_requested_only_for_the_bound_session():
    from api.tests.test_voice_test_providers import plan_fixture

    _, packets, plan = plan_fixture(signing_secret=SECRET)
    data = metadata()
    data["provider_plan"] = plan
    session = VoiceSession.parse(data, SESSION, 185)

    def handle(request):
        assert str(request.url) == CONTROL_URL
        assert json.loads(request.content) == {
            "operation": "get_providers",
            "session_id": SESSION,
            "lease_token": LEASE,
            "workflow_id": 185,
        }
        assert request.headers["x-sysevo-secret"] == SECRET
        return httpx.Response(
            200,
            json={
                "status": "providers_ready",
                "session_id": SESSION,
                "workflow_id": 185,
                "providers": packets,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert (
            await VoiceControlClient(http, SECRET).providers(session, LEASE) == packets
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "no_plan",
        "wrong_session",
        "wrong_workflow",
        "duplicate",
        "missing",
        "extra_field",
        "signature",
        "revision",
        "unexpected_provider",
    ],
)
async def test_provider_response_must_match_the_exact_saved_plan(bad):
    from api.tests.test_voice_test_providers import plan_fixture, sign

    _, packets, plan = plan_fixture(signing_secret=SECRET)
    data = metadata()
    if bad != "no_plan":
        data["provider_plan"] = plan
    session = VoiceSession.parse(data, SESSION, 185)
    response = {
        "status": "providers_ready",
        "session_id": SESSION,
        "workflow_id": 185,
        "providers": packets,
    }
    if bad == "wrong_session":
        response["session_id"] = RUN
    elif bad == "wrong_workflow":
        response["workflow_id"] = True
    elif bad == "duplicate":
        packets[1] = copy.deepcopy(packets[0])
    elif bad == "missing":
        packets.pop()
    elif bad == "extra_field":
        response["raw_vault_secret"] = "must_not_be_returned"
    elif bad == "signature":
        packets[0]["fingerprint"] = "0" * 64
    elif bad == "revision":
        packets[0]["revision"] += 1
        packets[0] = sign(packets[0], SECRET)
    elif bad == "unexpected_provider":
        packets[0]["service_type"] = "embeddings"
        packets[0] = sign(packets[0], SECRET)
    called = []

    def handle(request):
        called.append(True)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(VoiceControlError):
            await VoiceControlClient(http, SECRET).providers(session, LEASE)
    if bad == "no_plan":
        assert not called
