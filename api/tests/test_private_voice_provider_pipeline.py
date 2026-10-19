"""Exercise the real runtime credential gate without constructing provider SDKs."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.schemas.user_configuration import UserConfiguration
from api.services.workflow.test_sandbox_binding import make_sandbox_workflow_binding
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION
from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_providers import (
    PrivateVoiceProviderConfiguration,
    seal_private_voice_provider_configuration,
    validate_private_voice_provider_configuration,
)
from api.tests.test_voice_test_control import RUN, SESSION
from api.tests.test_voice_test_job import setup
from api.tests.test_voice_test_providers import (
    ALL_PROVIDERS,
    CLIENT_KEY,
    QA_KEY,
    SIGNING_SECRET,
    bindings,
    fixture,
    plan_fixture,
)


def runtime_fixture(realtime=False, embeddings=False):
    _, _, _, run, _, _, _ = setup()
    run.initial_context = {}
    marker = {
        "enabled": True,
        "source": "agent_testing_engine",
        "agent_test_run_id": RUN,
        "scenario_id": "voice_0",
        "voice_session_id": SESSION,
        "organization_id": 42,
        "workflow_binding": make_sandbox_workflow_binding(
            run.definition,
            workflow_id=185,
            workflow_run_id=123,
            run_id=RUN,
            scenario_id="voice_0",
            signing_secret=SIGNING_SECRET,
        ),
    }
    run.annotations = {SANDBOX_ANNOTATION: marker}
    sections = ["llm", "realtime"] if realtime else ["llm", "stt", "tts"]
    if embeddings:
        sections.append("embeddings")
    sources, packets = {}, []
    for name in sections:
        sources[name], packet = fixture(
            name, "openai_realtime" if name == "realtime" else "openai"
        )
        packets.append(packet)
    source = UserConfiguration(**sources, is_realtime=realtime)
    envelope = seal_private_voice_provider_configuration(
        source, packets, SIGNING_SECRET, bindings(packets), run
    )
    return run, envelope


def test_qa_credentials_exist_only_in_the_memory_envelope_and_not_the_run():
    run, envelope = runtime_fixture()
    configuration = validate_private_voice_provider_configuration(
        envelope, run, SIGNING_SECRET
    )
    assert configuration.llm.api_key == QA_KEY
    assert CLIENT_KEY not in envelope.encoded.decode()
    assert QA_KEY not in repr(envelope)
    assert QA_KEY not in repr(run)
    assert "configuration" not in run.annotations[SANDBOX_ANNOTATION]


@pytest.mark.parametrize("service,provider", ALL_PROVIDERS)
def test_every_registered_provider_survives_run_bound_credential_roundtrip(
    service, provider
):
    run, _ = runtime_fixture()
    source, packets, _ = plan_fixture(
        realtime=service == "realtime", embeddings=service == "embeddings"
    )
    setattr(source, service, fixture(service, provider)[0])
    packets = [packet for packet in packets if packet["service_type"] != service]
    packets.append(fixture(service, provider)[1])
    envelope = seal_private_voice_provider_configuration(
        source, packets, SIGNING_SECRET, bindings(packets), run
    )
    result = validate_private_voice_provider_configuration(
        envelope, run, SIGNING_SECRET
    )
    assert type(getattr(result, service)) is type(getattr(source, service))
    assert CLIENT_KEY not in envelope.encoded.decode()
    assert QA_KEY not in repr(envelope)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "workflow_id",
        "organization_id",
        "session_id",
        "scenario_id",
        "workflow_signature",
        "run_id",
    ],
)
def test_an_envelope_cannot_move_to_another_run_session_workflow_or_tenant(field):
    run, envelope = runtime_fixture()
    swapped = copy.deepcopy(run)
    marker = swapped.annotations[SANDBOX_ANNOTATION]
    if field in {"id", "workflow_id"}:
        setattr(swapped, field, 999)
    elif field == "workflow_signature":
        marker["workflow_binding"]["signature"] = "f" * 64
    elif field == "organization_id":
        marker[field] = 99
    elif field in {"session_id", "run_id"}:
        marker["voice_session_id" if field == "session_id" else "agent_test_run_id"] = (
            "99999999-9999-4999-8999-999999999999"
        )
    else:
        marker[field] = "different_scenario"
    with pytest.raises(VoiceControlError, match="runtime_binding_invalid"):
        validate_private_voice_provider_configuration(envelope, swapped, SIGNING_SECRET)


@pytest.mark.parametrize(
    "bad", ["none", "object", "credential", "signature", "type", "secret"]
)
def test_missing_or_changed_envelopes_cannot_be_used(bad):
    run, envelope = runtime_fixture()
    secret = SIGNING_SECRET
    if bad == "none":
        envelope = None
    elif bad == "object":
        envelope = UserConfiguration()
    elif bad == "credential":
        envelope = replace(
            envelope,
            encoded=envelope.encoded.replace(QA_KEY.encode(), CLIENT_KEY.encode()),
        )
    elif bad == "signature":
        envelope = replace(envelope, signature="0" * 64)
    elif bad == "type":
        envelope = PrivateVoiceProviderConfiguration("not bytes", "0" * 64)
    else:
        secret = "wrong-signer"
    with pytest.raises(VoiceControlError, match="runtime_binding_invalid"):
        validate_private_voice_provider_configuration(envelope, run, secret)


class ProviderBoundaryReached(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime", [False, True])
async def test_real_pipeline_reaches_factory_with_only_run_bound_qa_configuration(
    monkeypatch, realtime
):
    from api.services.configuration import org_provider_resolver
    from api.services.pipecat import run_pipeline as runtime
    from api.services.pipecat.audio_config import create_audio_config

    run, envelope = runtime_fixture(realtime)
    workflow = SimpleNamespace(
        id=185, organization_id=42, user_id=7, template_context_variables={}
    )
    scoped = AsyncMock(return_value=workflow)
    owner_read = AsyncMock(
        side_effect=AssertionError("must not resolve owner credentials")
    )
    org_read = AsyncMock(side_effect=AssertionError("must not resolve org credentials"))
    monkeypatch.setattr(runtime.db_client, "get_workflow", scoped)
    monkeypatch.setattr(runtime.db_client, "get_user_configurations", owner_read)
    for name in [
        "resolve_org_provider_config",
        "enrich_config_from_org_connections",
        "enrich_overrides_with_org_api_keys",
        "resolve_voice_for_tts",
    ]:
        monkeypatch.setattr(org_provider_resolver, name, org_read)
    factory = Mock(side_effect=ProviderBoundaryReached)
    for name in [
        "create_stt_service",
        "create_tts_service",
        "create_llm_service",
        "create_realtime_llm_service",
    ]:
        monkeypatch.setattr(runtime, name, factory)
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SIGNING_SECRET)
    with pytest.raises(ProviderBoundaryReached):
        await runtime._run_pipeline(
            object(),
            185,
            123,
            7,
            workflow_run=run,
            audio_config=create_audio_config("smallwebrtc"),
            expected_organization_id=42,
            private_voice_providers=envelope,
        )
    configuration = factory.call_args.args[0]
    for name in ["llm", "realtime"] if realtime else ["llm", "stt", "tts"]:
        assert getattr(configuration, name).api_key == QA_KEY
    assert CLIENT_KEY not in configuration.model_dump_json()
    owner_read.assert_not_awaited()
    org_read.assert_not_awaited()
    scoped.assert_awaited_once_with(185, organization_id=42)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["missing", "swapped", "unscoped", "owner_resolved", "telephony"]
)
async def test_real_pipeline_blocks_before_any_owner_read_or_sdk_factory(
    monkeypatch, bad
):
    from fastapi import HTTPException

    from api.services.pipecat import run_pipeline as runtime

    run, envelope = runtime_fixture()
    workflow = SimpleNamespace(id=185, organization_id=42, user_id=7)
    monkeypatch.setattr(
        runtime.db_client, "get_workflow", AsyncMock(return_value=workflow)
    )
    monkeypatch.setattr(
        runtime.db_client, "get_workflow_by_id", AsyncMock(return_value=workflow)
    )
    owner_read = AsyncMock()
    sdk = Mock()
    monkeypatch.setattr(runtime.db_client, "get_user_configurations", owner_read)
    for name in [
        "create_stt_service",
        "create_tts_service",
        "create_llm_service",
        "create_realtime_llm_service",
    ]:
        monkeypatch.setattr(runtime, name, sdk)
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SIGNING_SECRET)
    extra = {"expected_organization_id": 42, "private_voice_providers": envelope}
    if bad == "missing":
        extra["private_voice_providers"] = None
    elif bad == "swapped":
        extra["private_voice_providers"] = replace(envelope, signature="0" * 64)
    elif bad == "unscoped":
        extra["expected_organization_id"] = None
    elif bad == "owner_resolved":
        extra["resolved_user_config"] = UserConfiguration()
    else:
        extra["is_telephony"] = True
    with pytest.raises(HTTPException) as error:
        await runtime._run_pipeline(object(), 185, 123, 7, workflow_run=run, **extra)
    assert error.value.status_code == 409
    owner_read.assert_not_awaited()
    sdk.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_profile", [False, True])
async def test_private_embedding_resolution_cannot_call_org_or_global_fallback(
    monkeypatch, has_profile
):
    from api.services.pipecat import run_pipeline as runtime

    run, envelope = runtime_fixture(embeddings=has_profile)
    configuration = validate_private_voice_provider_configuration(
        envelope, run, SIGNING_SECRET
    )
    resolver = AsyncMock(side_effect=AssertionError("tenant fallback forbidden"))
    monkeypatch.setattr(runtime, "resolve_embeddings_config", resolver)
    result = await runtime._resolve_pipeline_embeddings(configuration, 42, True)
    assert result == (
        (
            QA_KEY,
            configuration.embeddings.model,
            getattr(configuration.embeddings, "base_url", None),
        )
        if has_profile
        else (None, None, None)
    )
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_embedding_resolution_preserves_the_existing_resolver(monkeypatch):
    from api.services.pipecat import run_pipeline as runtime

    configuration = UserConfiguration()
    resolver = AsyncMock(return_value=("normal-key", "normal-model", "normal-url"))
    monkeypatch.setattr(runtime, "resolve_embeddings_config", resolver)
    assert await runtime._resolve_pipeline_embeddings(configuration, 42, False) == (
        "normal-key",
        "normal-model",
        "normal-url",
    )
    resolver.assert_awaited_once_with(organization_id=42, user_config=configuration)


@pytest.mark.asyncio
async def test_a_live_call_cannot_use_a_private_test_credential_envelope(monkeypatch):
    from fastapi import HTTPException

    from api.services.pipecat import run_pipeline as runtime

    run, envelope = runtime_fixture()
    run.annotations = {}
    workflow = SimpleNamespace(id=185, organization_id=42, user_id=7)
    monkeypatch.setattr(
        runtime.db_client, "get_workflow_by_id", AsyncMock(return_value=workflow)
    )
    owner_read = AsyncMock()
    sdk = Mock()
    monkeypatch.setattr(runtime.db_client, "get_user_configurations", owner_read)
    monkeypatch.setattr(runtime, "create_llm_service", sdk)
    with pytest.raises(
        HTTPException, match="Test credentials require a private voice run"
    ):
        await runtime._run_pipeline(
            object(), 185, 123, 7, workflow_run=run, private_voice_providers=envelope
        )
    owner_read.assert_not_awaited()
    sdk.assert_not_called()
