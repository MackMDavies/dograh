"""Capture the native pipeline's behavior before selecting isolated QA accounts.

This is internal configuration evidence, not a provider or voice-quality test.
No SDK, call, configuration write or customer integration is started here.
"""

import copy
import hashlib
import hmac
import json

from api.services.configuration.pipeline import (
    apply_pipeline_call_configuration,
    resolve_pipeline_configuration,
    resolve_pipeline_voice,
)
from api.services.gen_ai.embedding.openai_service import DEFAULT_MODEL_ID
from api.services.gen_ai.embedding.resolver import (
    resolve_embedding_configuration_details,
)
from api.services.workflow.test_sandbox_binding import (
    SandboxWorkflowNotFoundError,
    resolve_requested_sandbox_definition,
)
from api.services.workflow.variable_resolution import (
    build_call_context,
    pick_template_variables,
)
from api.services.workflow.voice_test_control import VoiceControlError, positive_int
from api.services.workflow.voice_test_providers import (
    HASH,
    ROUTING_FIELDS,
    _canonical,
    _provider_class,
    provider_behavior_intent,
    provider_schema_fingerprint,
    validate_provider_behavior_intent,
)

INTENT_SCOPE = "sysevo_private_voice_intent_v1"
EMBEDDING_SOURCES = {
    "personal",
    "org_default",
    "org_embedding",
    "org_shared_key",
    "platform_embedding",
    "platform_shared_key",
    "unconfigured",
}
INTENT_FIELDS = {
    "version",
    "scope",
    "workflow_id",
    "organization_id",
    "owner_user_id",
    "workflow_version",
    "version_kind",
    "runtime_workflow_hash",
    "is_realtime",
    "timezone",
    "services",
    "schemas",
    "routing_fingerprints",
    "embedding_resolution",
    "production_credentials_verified",
    "production_routing_verified",
    "configuration_hash",
    "canonical_payload",
    "signature",
}


def _signature(secret, value):
    if not isinstance(secret, str) or not 0 < len(secret) <= 4096:
        raise VoiceControlError("voice_provider_intent_signer_unavailable")
    return hmac.new(secret.encode(), _canonical(value), hashlib.sha256).hexdigest()


def validate_voice_provider_intent(value, signing_secret):
    """Validate a saved internal snapshot without reading production configuration."""
    try:
        if (
            not isinstance(value, dict)
            or set(value) != INTENT_FIELDS
            or len(_canonical(value)) > 65536
        ):
            raise ValueError
        payload = value["canonical_payload"]
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > 32768:
            raise ValueError
        body = {
            key: item
            for key, item in value.items()
            if key not in {"signature", "configuration_hash", "canonical_payload"}
        }
        signed_body = json.loads(payload)
        if not isinstance(signed_body, dict) or signed_body != body:
            raise ValueError
        # JSON numbers are normalized by JS and jsonb. The retained UTF-8
        # payload is authoritative; signatures never depend on reserializing
        # a float such as 1.0 in another language.
        value = {
            **signed_body,
            **{
                key: value[key]
                for key in ("signature", "configuration_hash", "canonical_payload")
            },
        }
        if (
            not isinstance(value, dict)
            or set(value) != INTENT_FIELDS
            or value["version"] != 1
            or type(value["version"]) is not int
            or value["scope"] != INTENT_SCOPE
            or any(
                not positive_int(value[key])
                for key in (
                    "workflow_id",
                    "organization_id",
                    "owner_user_id",
                    "workflow_version",
                )
            )
            or value["version_kind"] not in {"draft", "published"}
            or type(value["is_realtime"]) is not bool
            or value["production_credentials_verified"] is not False
            or value["production_routing_verified"] is not False
            or not (
                value["timezone"] is None
                or isinstance(value["timezone"], str)
                and 0 < len(value["timezone"]) <= 128
            )
            or len(_canonical(value)) > 65536
        ):
            raise ValueError
        for name in ("runtime_workflow_hash", "configuration_hash", "signature"):
            if not isinstance(value[name], str) or not HASH.fullmatch(value[name]):
                raise ValueError
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            value["configuration_hash"], digest
        ) or not hmac.compare_digest(
            value["signature"],
            _signature(
                signing_secret,
                {
                    "scope": INTENT_SCOPE,
                    "canonical_payload": payload,
                    "configuration_hash": digest,
                },
            ),
        ):
            raise ValueError
        required = (
            {"llm", "realtime"} if value["is_realtime"] else {"llm", "stt", "tts"}
        )
        embedding = value["embedding_resolution"]
        if (
            not isinstance(embedding, dict)
            or set(embedding) != {"configured", "source"}
            or type(embedding["configured"]) is not bool
            or embedding["source"] not in EMBEDDING_SOURCES
            or embedding["configured"] != (embedding["source"] != "unconfigured")
        ):
            raise ValueError
        if embedding["configured"]:
            required.add("embeddings")
        for name in ("services", "schemas", "routing_fingerprints"):
            if not isinstance(value[name], dict) or set(value[name]) != required:
                raise ValueError
        for name in required:
            intent = validate_provider_behavior_intent(name, value["services"][name])
            if value["schemas"][name] != provider_schema_fingerprint(
                name, intent["provider"]
            ):
                raise ValueError
            routing = value["routing_fingerprints"][name]
            if not isinstance(routing, str) or not HASH.fullmatch(routing):
                raise ValueError
        return copy.deepcopy(value)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise VoiceControlError("voice_provider_intent_invalid") from None


async def capture_voice_provider_intent(
    db,
    *,
    workflow_id,
    organization_id,
    workflow_version,
    version_kind,
    runtime_workflow_hash,
    signing_secret,
):
    """Read ownership, pinned definition and actual inherited settings, then attest."""
    if (
        not all(
            positive_int(value)
            for value in (workflow_id, organization_id, workflow_version)
        )
        or version_kind not in {"draft", "published"}
        or not isinstance(runtime_workflow_hash, str)
        or not HASH.fullmatch(runtime_workflow_hash)
    ):
        raise VoiceControlError("voice_provider_intent_request_invalid", 400)
    # Check the signer before any credential-bearing DB operation.
    _signature(signing_secret, {"scope": INTENT_SCOPE})
    workflow = await db.get_workflow(workflow_id, organization_id=organization_id)
    if workflow is None:
        raise VoiceControlError("voice_workflow_not_found", 404)
    owner_user_id = workflow.user_id
    if (
        workflow.id != workflow_id
        or workflow.organization_id != organization_id
        or not positive_int(owner_user_id)
    ):
        raise VoiceControlError("voice_workflow_scope_changed")
    request = {
        "workflow_id": workflow_id,
        "organization_id": organization_id,
        "version_number": workflow_version,
        "version_kind": version_kind,
        "expected_hash": runtime_workflow_hash,
    }
    try:
        definition = await resolve_requested_sandbox_definition(db, **request)
        config = copy.deepcopy(definition.workflow_configurations or {})
        source = await resolve_pipeline_configuration(
            db, owner_user_id, organization_id, config.get("model_overrides")
        )
        template_variables = pick_template_variables(
            definition_vars=definition.template_context_variables,
            workflow_vars=workflow.template_context_variables,
        )
        source = apply_pipeline_call_configuration(
            source,
            config,
            build_call_context(
                initial_context={},
                extra_context_vars={},
                template_context_variables=template_variables,
            ),
        )
        source = await resolve_pipeline_voice(
            db,
            source,
            config.get("model_overrides"),
            organization_id,
            owner_user_id,
            strict_scope=True,
        )
        embedding = await resolve_embedding_configuration_details(
            organization_id, source, db=db
        )
        source = source.model_copy(deep=True)
        source.embeddings = None
        if embedding.api_key:
            model = _provider_class("embeddings", embedding.provider)
            # Live retrieval falls back to this model, irrespective of provider defaults.
            # An OpenRouter key with no endpoint currently goes to OpenAI; don't
            # silently test a corrected route and pass the broken live configuration.
            if embedding.provider == "openrouter" and not embedding.base_url:
                raise VoiceControlError("voice_embedding_route_incompatible")
            fields = {
                "provider": embedding.provider,
                "model": embedding.model or DEFAULT_MODEL_ID,
            }
            if "base_url" in model.model_fields:
                fields["base_url"] = (
                    embedding.base_url or model.model_fields["base_url"].default
                )
            source.embeddings = model.model_construct(**fields)
        # Reject a workflow edited, moved or reassigned while its configuration was read.
        current = await db.get_workflow(workflow_id, organization_id=organization_id)
        if (
            current is None
            or current.id != workflow_id
            or current.user_id != owner_user_id
            or current.organization_id != organization_id
        ):
            raise VoiceControlError("voice_workflow_scope_changed")
        await resolve_requested_sandbox_definition(db, **request)
    except SandboxWorkflowNotFoundError:
        raise VoiceControlError("voice_workflow_not_found", 404) from None
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        raise VoiceControlError(
            "voice_provider_configuration_changed_or_invalid"
        ) from None
    return attest_voice_provider_intent(
        source,
        workflow_id=workflow_id,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        workflow_version=workflow_version,
        version_kind=version_kind,
        runtime_workflow_hash=runtime_workflow_hash,
        embedding_source=embedding.source,
        signing_secret=signing_secret,
    )


def attest_voice_provider_intent(
    source,
    *,
    workflow_id,
    organization_id,
    owner_user_id,
    workflow_version,
    version_kind,
    runtime_workflow_hash,
    embedding_source,
    signing_secret,
):
    """Pure exporter shared by capture and protocol fixtures; never resolves credentials."""
    required = ["llm", "realtime"] if source.is_realtime else ["llm", "stt", "tts"]
    if source.embeddings is not None:
        required.append("embeddings")
    services, schemas, routes = {}, {}, {}
    for name in required:
        section = getattr(source, name)
        if section is None:
            raise VoiceControlError("voice_provider_configuration_incomplete")
        services[name] = provider_behavior_intent(name, section)
        schemas[name] = provider_schema_fingerprint(name, services[name]["provider"])
        routes[name] = _signature(
            signing_secret,
            {
                "scope": INTENT_SCOPE,
                "service_type": name,
                "routing": {
                    key: getattr(section, key)
                    for key in ROUTING_FIELDS
                    if key in type(section).model_fields
                },
            },
        )
    body = {
        "version": 1,
        "scope": INTENT_SCOPE,
        "workflow_id": workflow_id,
        "organization_id": organization_id,
        "owner_user_id": owner_user_id,
        "workflow_version": workflow_version,
        "version_kind": version_kind,
        "runtime_workflow_hash": runtime_workflow_hash,
        "is_realtime": source.is_realtime,
        "timezone": source.timezone,
        "services": services,
        "schemas": schemas,
        "routing_fingerprints": routes,
        "embedding_resolution": {
            "configured": source.embeddings is not None,
            "source": embedding_source,
        },
        "production_credentials_verified": False,
        "production_routing_verified": False,
    }
    payload = _canonical(body).decode("utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    attestation = {
        "scope": INTENT_SCOPE,
        "canonical_payload": payload,
        "configuration_hash": digest,
    }
    return validate_voice_provider_intent(
        {**body, **attestation, "signature": _signature(signing_secret, attestation)},
        signing_secret,
    )


def validate_voice_session_provider_plan(session, signing_secret, workflow=None):
    from api.services.workflow.voice_test_providers import (
        validate_private_voice_provider_plan,
    )

    plan = validate_private_voice_provider_plan(session.provider_plan)
    # These schemas are present in configuration/pricing, but their native
    # speech factory has no executable adapter. Reject before credentials/claim.
    if plan["services"].get("tts", {}).get("provider") in {"playht", "neets"}:
        raise VoiceControlError("voice_provider_runtime_unsupported")
    intent = validate_voice_provider_intent(session.provider_intent, signing_secret)
    if (
        intent["workflow_id"] != session.workflow_id
        or intent["workflow_version"] != session.workflow_version
        or intent["version_kind"] != session.version_kind
        or intent["runtime_workflow_hash"] != session.runtime_workflow_hash
        or intent["configuration_hash"] != session.provider_intent_hash
        or hashlib.sha256(_canonical(plan["bindings"])).hexdigest()
        != session.provider_binding_hash
        or plan["services"] != intent["services"]
        or plan["is_realtime"] != intent["is_realtime"]
        or plan["timezone"] != intent["timezone"]
        or workflow is not None
        and (
            intent["workflow_id"] != workflow.id
            or intent["organization_id"] != workflow.organization_id
            or intent["owner_user_id"] != workflow.user_id
        )
    ):
        raise VoiceControlError("voice_provider_intent_binding_invalid")
    return plan


async def validate_current_voice_provider_source(db, workflow, session, signing_secret):
    plan = validate_voice_session_provider_plan(session, signing_secret, workflow)
    current = await capture_voice_provider_intent(
        db,
        workflow_id=workflow.id,
        organization_id=workflow.organization_id,
        workflow_version=session.workflow_version,
        version_kind=session.version_kind,
        runtime_workflow_hash=session.runtime_workflow_hash,
        signing_secret=signing_secret,
    )
    if current["configuration_hash"] != session.provider_intent_hash:
        raise VoiceControlError("voice_provider_configuration_changed")
    return plan
