"""Strict, signed QA provider configuration; no SDK calls or tenant fallback.

The internal voice controller must issue these packets from versioned, Sysevo
test-only Vault entries. This module never resolves an org's production keys.
"""

import copy
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.masking import contains_masked_key
from api.services.configuration.registry import REGISTRY, ServiceType
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION
from api.services.workflow.voice_test_control import (
    VoiceControlError,
    canonical_uuid,
    positive_int,
)

SCOPE = "sysevo_private_voice_v1"
BEHAVIOR_FIELDS = frozenset(
    {
        "model",
        "voice",
        "language",
        "speed",
        "similarity_boost",
        "stability",
        "style",
        "use_speaker_boost",
        "volume",
        "latency",
        "normalize",
        "output_format",
        "prosody_speed",
        "prosody_volume",
        "temperature",
        "top_p",
    }
)
SECRET_FIELDS = frozenset(
    {"api_key", "credentials", "aws_access_key", "aws_secret_key"}
)
ROUTING_FIELDS = frozenset(
    {
        "base_url",
        "endpoint",
        "aws_region",
        "region",
        "location",
        "project_id",
        "group_id",
        "user_id",
    }
)
SECTIONS = {
    "llm": ServiceType.LLM,
    "tts": ServiceType.TTS,
    "stt": ServiceType.STT,
    "embeddings": ServiceType.EMBEDDINGS,
    "realtime": ServiceType.REALTIME,
}
AWS = {"aws_bedrock", "aws_polly", "aws_transcribe"}
HASH = re.compile(r"^[0-9a-f]{64}$")


def _invalid(code="voice_test_provider_invalid"):
    return VoiceControlError(code)


def _provider_class(service_type, provider):
    cls = REGISTRY.get(SECTIONS.get(service_type), {}).get(provider)
    if cls is None or set(cls.model_fields) - (
        BEHAVIOR_FIELDS | SECRET_FIELDS | ROUTING_FIELDS | {"provider"}
    ):
        raise _invalid("voice_test_provider_schema_unsupported")
    return cls


def provider_schema_fingerprint(service_type, provider):
    cls = _provider_class(service_type, provider)
    fields = {
        name: {
            "type": str(field.annotation),
            "required": field.is_required(),
            "category": "secret"
            if name in SECRET_FIELDS
            else "routing"
            if name in ROUTING_FIELDS
            else "behavior",
        }
        for name, field in cls.model_fields.items()
    }
    payload = {
        "version": 1,
        "scope": SCOPE,
        "service_type": service_type,
        "provider": provider,
        "fields": fields,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def validate_provider_behavior_intent(service_type, value):
    """Validate complete behavior without needing or retaining production keys."""
    try:
        if not isinstance(value, dict) or not isinstance(value.get("provider"), str):
            raise _invalid("voice_test_provider_behavior_invalid")
        model = _provider_class(service_type, value["provider"])
        names = BEHAVIOR_FIELDS & set(model.model_fields)
        if set(value) != {"provider"} | names:
            raise _invalid("voice_test_provider_behavior_invalid")
        result = {"provider": value["provider"]}
        for name in names:
            adapter = TypeAdapter(model.model_fields[name].rebuild_annotation())
            item = adapter.validate_python(value[name], strict=True)
            if isinstance(item, str) and (
                not item or len(item) > 4096 or re.search(r"[\x00-\x1f\x7f]", item)
                or "://" in item and not (
                    name == "voice" and value["provider"] == "playht"
                    and item.startswith("s3://voice-cloning-zero-shot/")
                )
            ):
                raise _invalid("voice_test_provider_behavior_invalid")
            result[name] = adapter.dump_python(item, mode="json")
        if len(_canonical(result)) > 16384:
            raise _invalid("voice_test_provider_behavior_invalid")
        return result
    except VoiceControlError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise _invalid("voice_test_provider_behavior_invalid") from None


def provider_behavior_intent(service_type, source):
    """Export only fields which may be copied into a private QA configuration."""
    provider = getattr(source, "provider", None)
    provider = getattr(provider, "value", provider)
    if not isinstance(provider, str):
        raise _invalid("voice_test_provider_behavior_invalid")
    model = _provider_class(service_type, provider)
    if type(source) is not model or getattr(source, "model_extra", None):
        raise _invalid("voice_test_provider_behavior_invalid")
    return validate_provider_behavior_intent(service_type, {
        "provider": provider,
        **{name: getattr(source, name) for name in BEHAVIOR_FIELDS & set(model.model_fields)},
    })


def _key(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and not re.search(r"[\s\x00-\x1f\x7f]", value)
        and not contains_masked_key(value)
    )


def _google_credentials(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 16384:
        raise _invalid()
    try:
        data = json.loads(value)
        if (
            not isinstance(data, dict)
            or data.get("type") != "service_account"
            or not (
                isinstance(data.get("private_key"), str)
                and data["private_key"].strip()
                and not contains_masked_key(data["private_key"])
                and isinstance(data.get("client_email"), str)
                and data["client_email"].endswith(".iam.gserviceaccount.com")
                and data.get("token_uri", "https://oauth2.googleapis.com/token")
                == "https://oauth2.googleapis.com/token"
                and data.get("universe_domain", "googleapis.com") == "googleapis.com"
            )
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise _invalid() from None


@dataclass(frozen=True, repr=False)
class TestProviderConnection:
    id: str
    revision: int
    service_type: str
    provider: str
    schema_fingerprint: str
    fingerprint: str
    settings: dict
    secrets: dict

    def __repr__(self):
        return "TestProviderConnection()"

    @classmethod
    def parse(cls, value, signing_secret):
        try:
            fields = {
                "id",
                "revision",
                "service_type",
                "provider",
                "settings",
                "secrets",
                "schema_fingerprint",
                "fingerprint",
                "scope",
            }
            if (
                not isinstance(value, dict)
                or set(value) != fields
                or value["scope"] != SCOPE
            ):
                raise _invalid()
            if len(_canonical(value)) > 32768:
                raise _invalid("voice_test_provider_payload_too_large")
            canonical_uuid(value["id"])
            if (
                not positive_int(value["revision"])
                or not isinstance(value["service_type"], str)
                or not isinstance(value["provider"], str)
            ):
                raise _invalid()
            model = _provider_class(value["service_type"], value["provider"])
            if value["schema_fingerprint"] != provider_schema_fingerprint(
                value["service_type"], value["provider"]
            ):
                raise _invalid("voice_test_provider_schema_changed")
            if not isinstance(value["settings"], dict) or not isinstance(
                value["secrets"], dict
            ):
                raise _invalid()
            if set(value["settings"]) - (
                ROUTING_FIELDS & set(model.model_fields)
            ) or set(value["secrets"]) - (SECRET_FIELDS & set(model.model_fields)):
                raise _invalid()
            if any(
                not isinstance(item, str)
                or not 0 < len(item) <= 4096
                or item.strip() != item
                or re.search(r"[\x00-\x1f\x7f]", item)
                for item in value["settings"].values()
            ):
                raise _invalid()
            for name, item in value["secrets"].items():
                if name == "credentials":
                    _google_credentials(item)
                elif name == "api_key" and isinstance(item, list):
                    if (
                        not 1 <= len(item) <= 5
                        or not all(_key(key) for key in item)
                        or len(set(item)) != len(item)
                    ):
                        raise _invalid()
                elif not _key(item):
                    raise _invalid()
            if (
                not isinstance(signing_secret, str)
                or not signing_secret
                or len(signing_secret) > 4096
            ):
                raise _invalid("voice_test_provider_signature_invalid")
            fingerprint = value["fingerprint"]
            if not isinstance(fingerprint, str) or not HASH.fullmatch(fingerprint):
                raise _invalid("voice_test_provider_signature_invalid")
            signed = {key: item for key, item in value.items() if key != "fingerprint"}
            expected = hmac.new(
                signing_secret.encode(), _canonical(signed), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(fingerprint, expected):
                raise _invalid("voice_test_provider_signature_invalid")
            return cls(
                value["id"],
                value["revision"],
                value["service_type"],
                value["provider"],
                value["schema_fingerprint"],
                fingerprint,
                copy.deepcopy(value["settings"]),
                copy.deepcopy(value["secrets"]),
            )
        except VoiceControlError:
            raise
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            RecursionError,
            OverflowError,
        ):
            # JSON, schema and validation exceptions must not retain raw credentials.
            raise _invalid() from None


def _routing_url(value, field, model, provider):
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        default = model.model_fields[field].default
        default_host = urlsplit(default).hostname if isinstance(default, str) else None
        allowed = (
            host == default_host
            or host == "sysevo.io"
            or host.endswith(".sysevo.io")
            or provider == "azure"
            and host.endswith(".openai.azure.com")
            or provider == "elevenlabs"
            and re.fullmatch(r"api\.(?:[a-z]{2}\.)?residency\.elevenlabs\.io", host)
        )
        if (
            url.scheme != "https"
            or not allowed
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.port not in (None, 443)
            or host.endswith(".")
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise _invalid("voice_test_provider_endpoint_invalid") from None


def _build_private_provider_section(service_type, source, connection):
    """Retain behavior only; replace all credential/account/routing fields with QA values."""
    try:
        if (
            not isinstance(connection, TestProviderConnection)
            or connection.service_type != service_type
        ):
            raise _invalid("voice_test_provider_binding_invalid")
        model = _provider_class(service_type, connection.provider)
        provider = getattr(
            getattr(source, "provider", None),
            "value",
            getattr(source, "provider", None),
        )
        if (
            type(source) is not model
            or provider != connection.provider
            or getattr(source, "model_extra", None)
        ):
            raise _invalid("voice_test_provider_binding_invalid")
        values = {
            name: copy.deepcopy(getattr(source, name))
            for name in BEHAVIOR_FIELDS
            if name in model.model_fields
        }
        values.update(copy.deepcopy(connection.settings))
        values.update(copy.deepcopy(connection.secrets))
        values["provider"] = connection.provider
        cloud_google = connection.provider in {
            "google_vertex",
            "google_vertex_realtime",
        } or (connection.provider == "google" and service_type in {"stt", "tts"})
        if connection.provider in AWS:
            if not _key(values.get("aws_access_key")) or not _key(
                values.get("aws_secret_key")
            ):
                raise _invalid("voice_test_provider_credentials_missing")
            values["api_key"] = None
        elif cloud_google:
            _google_credentials(values.get("credentials"))
            values["api_key"] = None
        elif not (
            isinstance(values.get("api_key"), list)
            and values["api_key"]
            or _key(values.get("api_key"))
        ):
            raise _invalid("voice_test_provider_credentials_missing")
        result = model(**values)
        for name in ("base_url", "endpoint"):
            if name in model.model_fields:
                _routing_url(getattr(result, name), name, model, connection.provider)
        voice = getattr(result, "voice", None)
        if (
            isinstance(voice, str)
            and "://" in voice
            and not (
                connection.provider == "playht"
                and voice.startswith("s3://voice-cloning-zero-shot/")
            )
        ):
            raise _invalid("voice_test_provider_voice_reference_invalid")
        return result
    except VoiceControlError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        raise _invalid("voice_test_provider_configuration_invalid") from None


def build_private_provider_section(service_type, source, packet, signing_secret):
    connection = TestProviderConnection.parse(packet, signing_secret)
    return _build_private_provider_section(service_type, source, connection)


def build_private_user_configuration(
    source, packets, signing_secret, expected_bindings
):
    """Build only the services this pipeline uses, with no original credential sections left over."""
    if (
        not isinstance(source, UserConfiguration)
        or not isinstance(packets, (list, tuple))
        or not isinstance(expected_bindings, dict)
    ):
        raise _invalid()
    by_service = {}
    for packet in packets:
        connection = TestProviderConnection.parse(packet, signing_secret)
        if (
            not isinstance(connection, TestProviderConnection)
            or connection.service_type in by_service
        ):
            raise _invalid("voice_test_provider_binding_invalid")
        expected = expected_bindings.get(connection.service_type)
        if (
            not isinstance(expected, dict)
            or set(expected) != {"id", "revision", "fingerprint"}
            or type(expected["revision"]) is not int
            or expected["id"] != connection.id
            or expected["revision"] != connection.revision
            or expected["fingerprint"] != connection.fingerprint
        ):
            raise _invalid("voice_test_provider_binding_invalid")
        by_service[connection.service_type] = connection
    required = ["llm", "realtime"] if source.is_realtime else ["llm", "stt", "tts"]
    if source.embeddings is not None:
        required.append("embeddings")
    if set(by_service) != set(required) or set(expected_bindings) != set(required):
        raise _invalid("voice_test_provider_credentials_missing")
    values = {
        "is_realtime": source.is_realtime,
        "timezone": source.timezone,
        "test_phone_number": None,
    }
    for name in required:
        values[name] = _build_private_provider_section(
            name, getattr(source, name), by_service[name]
        )
    return UserConfiguration(**values)


def validate_private_voice_provider_plan(value):
    """The saved plan contains complete behavior and profile references, never keys.

    Requiring every behavior field prevents native defaults or an owner's changed
    configuration from silently changing the agent being tested after planning.
    """
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"version", "scope", "is_realtime", "timezone", "services", "bindings"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["scope"] != SCOPE
            or type(value["is_realtime"]) is not bool
            or not isinstance(value["services"], dict)
            or not isinstance(value["bindings"], dict)
            or len(_canonical(value)) > 65536
            or not (
                value["timezone"] is None
                or isinstance(value["timezone"], str)
                and 0 < len(value["timezone"]) <= 128
            )
        ):
            raise _invalid("voice_test_provider_plan_invalid")
        required = (
            {"llm", "realtime"} if value["is_realtime"] else {"llm", "stt", "tts"}
        )
        if "embeddings" in value["services"]:
            required.add("embeddings")
        if set(value["services"]) != required or set(value["bindings"]) != required:
            raise _invalid("voice_test_provider_plan_invalid")
        for name, intent in value["services"].items():
            if not isinstance(intent, dict) or not isinstance(
                intent.get("provider"), str
            ):
                raise _invalid("voice_test_provider_plan_invalid")
            model = _provider_class(name, intent["provider"])
            if set(intent) != {"provider"} | (
                BEHAVIOR_FIELDS & set(model.model_fields)
            ):
                raise _invalid("voice_test_provider_plan_invalid")
            validate_provider_behavior_intent(name, intent)
            binding = value["bindings"][name]
            if (
                not isinstance(binding, dict)
                or set(binding) != {"id", "revision", "fingerprint"}
                or not positive_int(binding["revision"])
                or not isinstance(binding["fingerprint"], str)
                or not HASH.fullmatch(binding["fingerprint"])
            ):
                raise _invalid("voice_test_provider_plan_invalid")
            canonical_uuid(binding["id"])
        return copy.deepcopy(value)
    except VoiceControlError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid("voice_test_provider_plan_invalid") from None


def private_voice_source_from_plan(value):
    plan = validate_private_voice_provider_plan(value)
    sections = {}
    for name, intent in plan["services"].items():
        model = _provider_class(name, intent["provider"])
        # No owner credentials are read. This temporary behavior-only object
        # never reaches an SDK: build_private_user_configuration constructs and
        # validates each complete provider config with signed QA credentials.
        sections[name] = model.model_construct(**copy.deepcopy(intent))
    return UserConfiguration.model_construct(
        **sections, is_realtime=plan["is_realtime"], timezone=plan["timezone"]
    )


@dataclass(frozen=True, repr=False)
class PrivateVoiceProviderConfiguration:
    """In-memory only. Never persist, log, enqueue or return this credential packet."""

    encoded: bytes
    signature: str

    def __repr__(self):
        return "PrivateVoiceProviderConfiguration()"


def _runtime_identity(run):
    try:
        marker = run.annotations[SANDBOX_ANNOTATION]
        binding = marker["workflow_binding"]
        if (
            marker.get("enabled") is not True
            or marker.get("source") != "agent_testing_engine"
            or not positive_int(marker["organization_id"])
            or not isinstance(binding, dict)
            or not isinstance(binding.get("signature"), str)
            or not HASH.fullmatch(binding["signature"])
        ):
            raise _invalid()
        canonical_uuid(marker["voice_session_id"])
        canonical_uuid(marker["agent_test_run_id"])
        if not positive_int(run.id) or not positive_int(run.workflow_id):
            raise _invalid()
        return {
            "scope": SCOPE,
            "version": 1,
            "session_id": marker["voice_session_id"],
            "agent_test_run_id": marker["agent_test_run_id"],
            "scenario_id": marker["scenario_id"],
            "organization_id": marker["organization_id"],
            "workflow_id": run.workflow_id,
            "workflow_run_id": run.id,
            "workflow_signature": binding["signature"],
        }
    except (KeyError, TypeError, AttributeError):
        raise _invalid("voice_test_provider_runtime_binding_invalid") from None


def seal_private_voice_provider_configuration(
    source, packets, signing_secret, expected_bindings, run
):
    """Validate signed QA profiles before sealing them to an attested runtime run.

    The caller must already have checked the run's workflow attestation. Only
    configuration built by the strict QA resolver can enter this envelope.
    """
    configuration = build_private_user_configuration(
        source, packets, signing_secret, expected_bindings
    )
    encoded = _canonical(
        {
            **_runtime_identity(run),
            "configuration": configuration.model_dump(mode="json"),
        }
    )
    if len(encoded) > 196608:
        raise _invalid("voice_test_provider_payload_too_large")
    return PrivateVoiceProviderConfiguration(
        encoded,
        hmac.new(signing_secret.encode(), encoded, hashlib.sha256).hexdigest(),
    )


def validate_private_voice_provider_configuration(envelope, run, signing_secret):
    """Reject missing, swapped or mutated credentials before constructing SDKs."""
    try:
        if (
            type(envelope) is not PrivateVoiceProviderConfiguration
            or not isinstance(envelope.encoded, bytes)
            or not 0 < len(envelope.encoded) <= 196608
            or not isinstance(envelope.signature, str)
            or not HASH.fullmatch(envelope.signature)
            or not isinstance(signing_secret, str)
            or not signing_secret
        ):
            raise _invalid("voice_test_provider_runtime_binding_invalid")
        expected = hmac.new(
            signing_secret.encode(), envelope.encoded, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, envelope.signature):
            raise _invalid("voice_test_provider_runtime_binding_invalid")
        value = json.loads(envelope.encoded)
        identity = _runtime_identity(run)
        if (
            not isinstance(value, dict)
            or set(value) != {*identity, "configuration"}
            or any(value[key] != item for key, item in identity.items())
        ):
            raise _invalid("voice_test_provider_runtime_binding_invalid")
        configuration = UserConfiguration.model_validate(value["configuration"])
        # Pydantic silently ignores unknown fields by default. Round-trip equality
        # prevents a signed configuration from hiding ignored credential sections.
        if configuration.model_dump(mode="json") != value["configuration"]:
            raise _invalid("voice_test_provider_runtime_binding_invalid")
        return configuration
    except VoiceControlError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid("voice_test_provider_runtime_binding_invalid") from None


def private_voice_embedding_configuration(configuration):
    """A missing QA embedding profile must never reach the org/global resolver."""
    embedding = configuration.embeddings
    if embedding is None:
        return None, None, None
    # The QA resolver validates pools, but retrieval expects a single key.
    keys = embedding.api_key
    key = keys[0] if isinstance(keys, list) else keys
    if not _key(key):
        raise _invalid("voice_test_provider_credentials_missing")
    return key, embedding.model, getattr(embedding, "base_url", None)
