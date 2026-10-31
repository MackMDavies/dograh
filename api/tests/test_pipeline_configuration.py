from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration import org_provider_resolver as resolver
from api.services.configuration.pipeline import (
    apply_agent_language_configuration,
    apply_pipeline_call_configuration,
    build_agent_language_instruction,
    resolve_pipeline_configuration,
    resolve_pipeline_voice,
)
from api.tests.test_voice_test_providers import fixture, plan_fixture


@pytest.mark.parametrize(
    "language,expected",
    [("Spanish", "es"), ("es-ES", "es"), ("en-GB", "en")],
)
def test_agent_language_is_applied_to_realtime_and_provider_settings(language, expected):
    source, _, _ = plan_fixture()
    source.is_realtime = True
    source.realtime = fixture("realtime", "elevenlabs_realtime")[0]
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "flux-general-en"
    before = source.model_dump()

    result = apply_agent_language_configuration(source, {"language": language})

    assert result is not source
    assert result.realtime.language == expected
    if "language" in result.tts.model_fields:
        assert result.tts.language == (language if "-" in language else expected)
    assert source.model_dump() == before
    if expected != "en":
        assert result.stt.model == "flux-general-multi"
        assert result.stt.language == source.stt.language


def test_multilingual_flux_languages_keep_flux_turn_detection():
    source, _, _ = plan_fixture()
    source.is_realtime = True
    source.realtime = fixture("realtime", "elevenlabs_realtime")[0]
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "flux-general-en"

    result = apply_agent_language_configuration(
        source,
        {"language": "en-GB, es-ES", "multilingual_mode": True},
    )

    assert result.stt.model == "flux-general-multi"
    assert result.stt.language == source.stt.language
    assert result.realtime.language is None
    assert "Detect which of these languages" in build_agent_language_instruction(
        {"language": "en-GB, es-ES", "multilingual_mode": True}
    )


def test_language_outside_flux_multilingual_set_uses_nova3():
    source, _, _ = plan_fixture()
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "flux-general-en"

    result = apply_agent_language_configuration(source, {"language": "Arabic"})

    assert result.stt.model == "nova-3-general"
    assert result.stt.language == "ar"


def test_multilingual_language_outside_flux_set_uses_nova3_auto_detection():
    source, _, _ = plan_fixture()
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "flux-general-en"

    result = apply_agent_language_configuration(
        source,
        {"language": "en-GB, ar-SA", "multilingual_mode": True},
    )

    assert result.stt.model == "nova-3-general"
    assert result.stt.language == "multi"


def test_non_realtime_deepgram_language_is_applied_to_nova2():
    source, _, _ = plan_fixture()
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "nova-2-general"

    result = apply_agent_language_configuration(source, {"language": "Spanish"})

    assert result.stt.model == "nova-2-general"
    assert result.stt.language == "es"


def test_multilingual_nova2_selection_upgrades_to_nova3():
    source, _, _ = plan_fixture()
    source.stt = fixture("stt", "deepgram")[0]
    source.stt.model = "nova-2-general"

    result = apply_agent_language_configuration(
        source,
        {"language": "en-GB, es-ES", "multilingual_mode": True},
    )

    assert result.stt.model == "nova-3-general"
    assert result.stt.language == "multi"


def test_agent_without_language_keeps_provider_configuration_unchanged():
    source, _, _ = plan_fixture()
    before = source.model_dump()

    assert apply_agent_language_configuration(source, {}) is source
    assert source.model_dump() == before


@pytest.mark.parametrize("provider", ["openai", "elevenlabs"])
@pytest.mark.parametrize("syra", [False, True])
@pytest.mark.parametrize(
    "speed,expected", [(1.13, 1.13), (4.0, 1.2), (True, None), (float("nan"), None)]
)
def test_call_overrides_preserve_syra_speed_scope_and_input(
    provider, syra, speed, expected
):
    source, _, _ = plan_fixture()
    source.tts = fixture("tts", provider)[0]
    before = source.model_dump()
    result = apply_pipeline_call_configuration(
        source, {"syra_voice": syra}, {"syra_speech_speed": speed}
    )
    applied = syra and provider == "elevenlabs" and expected is not None
    assert result.tts.speed == (expected if applied else source.tts.speed)
    assert source.model_dump() == before
    assert (result is source) is not applied


@pytest.mark.asyncio
@pytest.mark.parametrize("organization_id", [None, 42])
async def test_shared_pipeline_resolution_preserves_live_precedence_and_inputs(
    monkeypatch, organization_id
):
    source, _, _ = plan_fixture()
    before = source.model_dump()
    db = SimpleNamespace(get_user_configurations=AsyncMock(return_value=source))
    order = []

    async def org_defaults(org, config):
        assert org == 42 and config is source
        order.append("org_defaults")
        return config

    async def enrich(org, config):
        assert org == 42 and config is source
        order.append("enrich")
        return config

    async def enrich_overrides(overrides, org):
        assert org == 42
        order.append("overrides")
        return {**overrides, "llm": {**overrides["llm"], "temperature": 0.25}}

    defaults = AsyncMock(side_effect=org_defaults)
    enrichment = AsyncMock(side_effect=enrich)
    overrides_lookup = AsyncMock(side_effect=enrich_overrides)
    monkeypatch.setattr(resolver, "resolve_org_provider_config", defaults)
    monkeypatch.setattr(resolver, "enrich_config_from_org_connections", enrichment)
    monkeypatch.setattr(
        resolver, "enrich_overrides_with_org_api_keys", overrides_lookup
    )
    overrides = {"llm": {"model": "workflow-model"}}
    result = await resolve_pipeline_configuration(db, 7, organization_id, overrides)
    assert result.llm.model == "workflow-model"
    assert source.model_dump() == before
    assert overrides == {"llm": {"model": "workflow-model"}}
    assert order == (["org_defaults", "enrich", "overrides"] if organization_id else [])
    if organization_id:
        assert result.llm.temperature == 0.25
    else:
        defaults.assert_not_awaited()
        enrichment.assert_not_awaited()
        overrides_lookup.assert_not_awaited()
    db.get_user_configurations.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_empty_overrides_do_not_trigger_credential_enrichment(monkeypatch):
    source, _, _ = plan_fixture()
    db = SimpleNamespace(get_user_configurations=AsyncMock(return_value=source))
    monkeypatch.setattr(
        resolver, "resolve_org_provider_config", AsyncMock(return_value=source)
    )
    monkeypatch.setattr(
        resolver, "enrich_config_from_org_connections", AsyncMock(return_value=source)
    )
    enrichment = AsyncMock()
    monkeypatch.setattr(resolver, "enrich_overrides_with_org_api_keys", enrichment)
    result = await resolve_pipeline_configuration(db, 7, 42, None)
    assert result.model_dump() == source.model_dump()
    enrichment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("organization_id", [None, 42])
@pytest.mark.parametrize("resolved_voice", [None, "selected-provider-voice"])
async def test_shared_voice_resolution_preserves_lookup_fallback_and_source(
    monkeypatch, organization_id, resolved_voice
):
    source, _, _ = plan_fixture()
    original_voice = source.tts.voice
    lookup = AsyncMock(return_value=resolved_voice)
    monkeypatch.setattr(resolver, "resolve_voice_for_tts", lookup)
    db = SimpleNamespace(
        get_user_by_id=AsyncMock(
            return_value=SimpleNamespace(selected_organization_id=42)
        )
    )
    result = await resolve_pipeline_voice(
        db, source, {"tts": {"voice_uuid": "voice-library-uuid"}}, organization_id, 7
    )
    lookup.assert_awaited_once_with("voice-library-uuid", 42)
    if organization_id:
        db.get_user_by_id.assert_not_awaited()
    else:
        db.get_user_by_id.assert_awaited_once_with(7)
    assert source.tts.voice == original_voice
    assert result.tts.voice == (resolved_voice or original_voice)
    assert (result is source) is (resolved_voice is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_voice_resolution_skips_missing_tts_or_missing_uuid(monkeypatch, empty):
    source = UserConfiguration() if empty else plan_fixture()[0]
    lookup = AsyncMock()
    monkeypatch.setattr(resolver, "resolve_voice_for_tts", lookup)
    db = SimpleNamespace(get_user_by_id=AsyncMock())
    assert await resolve_pipeline_voice(db, source, {}, 42, 7) is source
    lookup.assert_not_awaited()
    db.get_user_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "own",
        "own_pending",
        "public",
        "foreign",
        "pending_public",
        "missing",
        "provider",
        "empty",
    ],
)
async def test_private_voice_planning_requires_owned_or_ready_public_matching_voice(
    monkeypatch, kind
):
    source, _, _ = plan_fixture()
    row = SimpleNamespace(
        organization_id=42 if kind in {"own", "own_pending"} else 99,
        is_public=kind in {"public", "pending_public", "provider", "empty"},
        status="pending" if kind in {"pending_public", "own_pending"} else "ready",
        provider="elevenlabs" if kind == "provider" else "openai",
        provider_voice_id="" if kind == "empty" else "selected-voice",
    )
    db = SimpleNamespace(
        get_voice_for_private_planning=AsyncMock(
            return_value=None if kind == "missing" else row
        )
    )
    unscoped = AsyncMock(
        side_effect=AssertionError(
            "private planning cannot search private voices across tenants"
        )
    )
    monkeypatch.setattr(resolver, "resolve_voice_for_tts", unscoped)
    call = resolve_pipeline_voice(
        db, source, {"tts": {"voice_uuid": "selected"}}, 42, 7, strict_scope=True
    )
    if kind in {"own", "public"}:
        result = await call
        assert result.tts.voice == "selected-voice"
    else:
        with pytest.raises(ValueError):
            await call
    unscoped.assert_not_awaited()
    db.get_voice_for_private_planning.assert_awaited_once_with("selected", 42)


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("already_resolved", [False, True])
async def test_live_pipeline_preserves_configuration_at_provider_boundary(
    monkeypatch, realtime, already_resolved
):
    """Exercise the actual pipeline without starting any provider SDK or call."""
    from api.services.pipecat import run_pipeline as runtime
    from api.services.pipecat.audio_config import create_audio_config
    from api.tests.test_voice_test_job import setup

    source, _, _ = plan_fixture(realtime=realtime)
    before = source.model_dump()
    _, _, _, run, _, _, _ = setup()
    run.initial_context = {}
    run.annotations = {}
    run.definition.workflow_configurations = {
        "model_overrides": {
            "llm": {"model": "pinned-workflow-model"},
            "tts": {"voice_uuid": "pinned-voice-uuid"},
        }
    }
    workflow = SimpleNamespace(
        id=185, organization_id=42, user_id=7, template_context_variables={}
    )
    monkeypatch.setattr(
        runtime.db_client, "get_workflow_by_id", AsyncMock(return_value=workflow)
    )
    owner_read = AsyncMock(return_value=source)
    monkeypatch.setattr(runtime.db_client, "get_user_configurations", owner_read)
    defaults = AsyncMock(return_value=source)
    enrichment = AsyncMock(return_value=source)
    override_enrichment = AsyncMock(side_effect=lambda overrides, org: overrides)
    voice_lookup = AsyncMock(return_value="pinned-provider-voice")
    monkeypatch.setattr(resolver, "resolve_org_provider_config", defaults)
    monkeypatch.setattr(resolver, "enrich_config_from_org_connections", enrichment)
    monkeypatch.setattr(
        resolver, "enrich_overrides_with_org_api_keys", override_enrichment
    )
    monkeypatch.setattr(resolver, "resolve_voice_for_tts", voice_lookup)

    class ProviderBoundaryReached(Exception):
        pass

    factory = Mock(side_effect=ProviderBoundaryReached)
    monkeypatch.setattr(
        runtime,
        "create_realtime_llm_service" if realtime else "create_stt_service",
        factory,
    )
    pre_resolved = source.model_copy(deep=True) if already_resolved else None
    if pre_resolved:
        pre_resolved.llm = pre_resolved.llm.model_copy(
            update={"model": "transport-resolved-model"}
        )
    with pytest.raises(ProviderBoundaryReached):
        await runtime._run_pipeline(
            object(),
            185,
            123,
            7,
            workflow_run=run,
            resolved_user_config=pre_resolved,
            audio_config=create_audio_config("smallwebrtc"),
        )
    effective = factory.call_args.args[0]
    assert effective.llm.model == (
        "transport-resolved-model" if already_resolved else "pinned-workflow-model"
    )
    if realtime:
        voice_lookup.assert_not_awaited()
    else:
        assert effective.tts.voice == "pinned-provider-voice"
        voice_lookup.assert_awaited_once_with("pinned-voice-uuid", 42)
    if already_resolved:
        owner_read.assert_not_awaited()
        defaults.assert_not_awaited()
        enrichment.assert_not_awaited()
        override_enrichment.assert_not_awaited()
    else:
        owner_read.assert_awaited_once_with(7)
        defaults.assert_awaited_once_with(42, source)
        enrichment.assert_awaited_once_with(42, source)
        override_enrichment.assert_awaited_once_with(
            run.definition.workflow_configurations["model_overrides"], 42
        )
    assert source.model_dump() == before
