"""Native source evidence: full provider behavior, version binding and no client keys."""

import asyncio
import copy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.configuration import org_provider_resolver
from api.services.gen_ai.embedding.resolver import ResolvedEmbeddingConfiguration
from api.services.workflow.sandbox_repair import workflow_snapshot_hash
from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_intent import (
    capture_voice_provider_intent,
    validate_voice_provider_intent,
)
from api.services.workflow.voice_test_providers import (
    BEHAVIOR_FIELDS,
    ROUTING_FIELDS,
    SECRET_FIELDS,
    provider_behavior_intent,
    provider_schema_fingerprint,
)
from api.tests.test_voice_test_providers import (
    ALL_PROVIDERS,
    CLIENT_KEY,
    SIGNING_SECRET,
    fixture,
    plan_fixture,
)


def setup(monkeypatch, *, realtime=False):
    source, _, _ = plan_fixture(realtime=realtime)
    overrides = {"llm": {"model": "pinned-purpose-model"}}
    if not realtime:
        overrides["tts"] = {"voice_uuid": "selected-voice-uuid"}
    definition = SimpleNamespace(
        workflow_id=185,
        version_number=3,
        status="published",
        workflow_json={"nodes": []},
        workflow_configurations={"model_overrides": overrides},
        template_context_variables={},
    )
    workflow = SimpleNamespace(
        id=185, organization_id=42, user_id=7, template_context_variables={}
    )
    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=workflow),
        get_workflow_versions=AsyncMock(return_value=[definition]),
        get_user_configurations=AsyncMock(return_value=source),
        list_connections=AsyncMock(return_value=[]),
        list_all_connections_superuser=AsyncMock(return_value=[]),
        get_connection_by_provider=AsyncMock(return_value=None),
        get_default_embedding_model_connection=AsyncMock(return_value=None),
        get_voice_for_private_planning=AsyncMock(
            return_value=SimpleNamespace(
                organization_id=42,
                provider="openai",
                provider_voice_id="selected-provider-voice",
                is_public=False,
                status="ready",
            )
        ),
    )
    monkeypatch.setattr(org_provider_resolver, "db_client", db)
    request = {
        "workflow_id": 185,
        "organization_id": 42,
        "workflow_version": 3,
        "version_kind": "published",
        "signing_secret": SIGNING_SECRET,
        "runtime_workflow_hash": workflow_snapshot_hash(
            definition.workflow_json, definition.workflow_configurations, {}
        ),
    }
    return db, source, definition, request


@pytest.mark.parametrize("service,provider", ALL_PROVIDERS)
def test_every_registered_schema_exports_complete_validated_behavior_only(
    service, provider
):
    source, _ = fixture(service, provider)
    before = source.model_dump()
    exported = provider_behavior_intent(service, source)
    assert set(exported) == {"provider"} | (
        BEHAVIOR_FIELDS & set(type(source).model_fields)
    )
    assert not (set(exported) & (SECRET_FIELDS | ROUTING_FIELDS))
    assert CLIENT_KEY not in json.dumps(exported)
    assert len(provider_schema_fingerprint(service, provider)) == 64
    assert source.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime", [False, True])
async def test_capture_uses_owner_and_pinned_definition_without_credentials_or_sdk(
    monkeypatch, realtime
):
    db, source, definition, request = setup(monkeypatch, realtime=realtime)
    before = source.model_dump()
    saved = copy.deepcopy(definition.workflow_configurations)
    result = await capture_voice_provider_intent(db, **request)
    assert result["owner_user_id"] == 7
    assert result["services"]["llm"]["model"] == "pinned-purpose-model"
    if not realtime:
        assert result["services"]["tts"]["voice"] == "selected-provider-voice"
    assert result["production_credentials_verified"] is False
    assert result["production_routing_verified"] is False
    assert result["embedding_resolution"] == {
        "configured": False,
        "source": "unconfigured",
    }
    assert set(result["services"]) == (
        {"llm", "realtime"} if realtime else {"llm", "stt", "tts"}
    )
    assert CLIENT_KEY not in json.dumps(result) and SIGNING_SECRET not in json.dumps(
        result
    )
    db.get_user_configurations.assert_awaited_once_with(7)
    assert all(
        call.kwargs == {"organization_id": 42}
        for call in db.get_workflow.await_args_list
    )
    assert source.model_dump() == before
    assert definition.workflow_configurations == saved
    assert validate_voice_provider_intent(result, SIGNING_SECRET) == result


@pytest.mark.asyncio
async def test_configured_syra_voice_settings_are_captured_with_the_live_rule(
    monkeypatch,
):
    db, source, definition, request = setup(monkeypatch)
    source.tts = fixture("tts", "elevenlabs")[0]
    definition.workflow_configurations["syra_voice"] = True
    definition.workflow_configurations["model_overrides"]["tts"] = {
        "voice_settings": {"speed": 1.13}
    }
    request["runtime_workflow_hash"] = workflow_snapshot_hash(
        definition.workflow_json,
        definition.workflow_configurations,
        definition.template_context_variables,
    )
    result = await capture_voice_provider_intent(db, **request)
    assert result["services"]["tts"]["speed"] == 1.13
    assert source.tts.speed != 1.13


@pytest.mark.asyncio
async def test_ownership_is_checked_before_versions_or_any_configuration(monkeypatch):
    db, _, _, request = setup(monkeypatch)
    db.get_workflow.return_value = None
    with pytest.raises(VoiceControlError, match="^voice_workflow_not_found$"):
        await capture_voice_provider_intent(db, **request)
    db.get_user_configurations.assert_not_awaited()
    db.get_workflow_versions.assert_not_awaited()
    db.get_default_embedding_model_connection.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version_hash", "owner", "organization", "removed"])
async def test_changes_during_capture_cannot_produce_valid_evidence(
    monkeypatch, change
):
    db, source, definition, request = setup(monkeypatch)

    async def owner_read(user):
        assert user == 7
        if change == "version_hash":
            definition.workflow_configurations["changed"] = True
        elif change == "owner":
            db.get_workflow.return_value.user_id = 8
        elif change == "organization":
            db.get_workflow.return_value.organization_id = 99
        else:
            db.get_workflow.return_value = None
        return source

    db.get_user_configurations.side_effect = owner_read
    with pytest.raises(VoiceControlError):
        await capture_voice_provider_intent(db, **request)


@pytest.mark.asyncio
async def test_route_changes_are_fingerprinted_without_returning_customer_endpoints(
    monkeypatch,
):
    db, source, _, request = setup(monkeypatch)
    first = await capture_voice_provider_intent(db, **request)
    private_endpoint = "https://private-customer.invalid/credential-in-path"
    changed = source.model_copy(deep=True)
    changed.llm = changed.llm.model_copy(update={"base_url": private_endpoint})
    db.get_user_configurations.return_value = changed
    second = await capture_voice_provider_intent(db, **request)
    assert first["services"] == second["services"]
    assert first["configuration_hash"] != second["configuration_hash"]
    assert first["routing_fingerprints"]["llm"] != second["routing_fingerprints"]["llm"]
    assert private_endpoint not in json.dumps(second)
    changed.llm = changed.llm.model_copy(update={"api_key": "qa-rotated-key"})
    assert await capture_voice_provider_intent(db, **request) == second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_name",
    [
        "personal",
        "org_default",
        "org_embedding",
        "org_shared_key",
        "platform_embedding",
        "platform_shared_key",
    ],
)
async def test_actual_embedding_model_and_fallback_origin_are_captured(
    monkeypatch, source_name
):
    from api.services.workflow import voice_test_intent as module

    db, _, _, request = setup(monkeypatch)
    monkeypatch.setattr(
        module,
        "resolve_embedding_configuration_details",
        AsyncMock(
            return_value=ResolvedEmbeddingConfiguration(
                CLIENT_KEY,
                None,
                None,
                "openai",
                source_name,
            )
        ),
    )
    result = await capture_voice_provider_intent(db, **request)
    assert result["services"]["embeddings"] == {
        "provider": "openai",
        "model": "text-embedding-3-small",
    }
    assert result["embedding_resolution"] == {"configured": True, "source": source_name}
    assert CLIENT_KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_broken_openrouter_fallback_is_reported_instead_of_testing_a_corrected_route(
    monkeypatch,
):
    from api.services.workflow import voice_test_intent as module

    db, _, _, request = setup(monkeypatch)
    monkeypatch.setattr(
        module,
        "resolve_embedding_configuration_details",
        AsyncMock(
            return_value=ResolvedEmbeddingConfiguration(
                CLIENT_KEY,
                None,
                None,
                "openrouter",
                "org_shared_key",
            )
        ),
    )
    with pytest.raises(VoiceControlError, match="^voice_embedding_route_incompatible$"):
        await capture_voice_provider_intent(db, **request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "workflow_id",
        "services",
        "schemas",
        "routing_fingerprints",
        "production_credentials_verified",
        "unexpected",
    ],
)
async def test_saved_evidence_cannot_be_mutated_or_rebound(monkeypatch, change):
    db, _, _, request = setup(monkeypatch)
    value = await capture_voice_provider_intent(db, **request)
    if change == "signature":
        value[change] = "0" * 64
    elif change == "workflow_id":
        value[change] = 186
    elif change in {"services", "schemas", "routing_fingerprints"}:
        value[change].pop("llm")
    else:
        value[change] = True
    with pytest.raises(VoiceControlError):
        validate_voice_provider_intent(value, SIGNING_SECRET)


@pytest.mark.asyncio
async def test_node_verifies_exact_signed_bytes_and_json_roundtrip_preserves_evidence(
    monkeypatch,
):
    db, _, definition, request = setup(monkeypatch)
    definition.workflow_configurations["model_overrides"]["llm"]["model"] = (
        "modèle-音-1"
    )
    request["runtime_workflow_hash"] = workflow_snapshot_hash(
        definition.workflow_json, definition.workflow_configurations, {}
    )
    snapshot = await capture_voice_provider_intent(db, **request)
    program = """
const crypto = require('node:crypto');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const { snapshot: value, secret } = JSON.parse(input);
  const digest = crypto.createHash('sha256').update(value.canonical_payload, 'utf8').digest('hex');
  const signed = JSON.stringify({ canonical_payload: value.canonical_payload, configuration_hash: digest, scope: value.scope });
  const signature = crypto.createHmac('sha256', secret).update(signed, 'utf8').digest('hex');
  if (digest !== value.configuration_hash || signature !== value.signature) process.exit(1);
  process.stdout.write(JSON.stringify(value));
});
"""
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", "-e", program],
        input=json.dumps({"snapshot": snapshot, "secret": SIGNING_SECRET}).encode(),
        capture_output=True,
        check=True,
        timeout=10,
    )
    roundtripped = json.loads(result.stdout)
    assert validate_voice_provider_intent(roundtripped, SIGNING_SECRET) == snapshot
    assert "1.0" in snapshot["canonical_payload"]
    assert snapshot["services"]["tts"]["speed"] == 1.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", {"private": CLIENT_KEY}),
        ("voice", "https://client.invalid/voice"),
        ("speed", float("nan")),
        ("speed", True),
    ],
)
def test_invalid_unvalidated_workflow_behavior_cannot_be_exported(field, value):
    source, _ = fixture("tts", "openai")
    source = source.model_copy(update={field: value})
    with pytest.raises(VoiceControlError):
        provider_behavior_intent("tts", source)
