"""Provider config isolation with synthetic keys; no live provider, DB or audio."""

import copy
import hashlib
import hmac
import json
import socket

import pytest

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import REGISTRY, ServiceType
from api.services.workflow.voice_test_control import VoiceControlError
from api.services.workflow.voice_test_providers import (
    BEHAVIOR_FIELDS,
    ROUTING_FIELDS,
    SCOPE,
    SECRET_FIELDS,
    SECTIONS,
    build_private_provider_section,
    build_private_user_configuration,
    provider_schema_fingerprint,
)
from api.services.workflow.voice_test_providers import (
    TestProviderConnection as ProviderConnection,
)

SIGNING_SECRET = "synthetic-native-test-signing-secret"
ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
CLIENT_KEY = "CLIENT_SECRET_NEVER_USE"
QA_KEY = "QA_TEST_SECRET"


def google_credentials(project):
    return json.dumps(
        {
            "type": "service_account",
            "project_id": project,
            "private_key": f"synthetic-private-key-{project}",
            "client_email": f"qa@{project}.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def sign(value, secret=SIGNING_SECRET):
    value = copy.deepcopy(value)
    value.pop("fingerprint", None)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    value["fingerprint"] = hmac.new(
        secret.encode(), encoded, hashlib.sha256
    ).hexdigest()
    return value


def fixture(service="llm", provider="openai"):
    model = REGISTRY[SECTIONS[service]][provider]
    client = {
        "api_key": CLIENT_KEY,
        "credentials": google_credentials("client-project"),
        "aws_access_key": "CLIENT_ACCESS_KEY",
        "aws_secret_key": CLIENT_KEY,
        "base_url": "https://customer.example.invalid/v1",
        "endpoint": "https://client-resource.openai.azure.com/",
        "aws_region": "us-west-2",
        "region": "westus",
        "location": "europe-west1",
        "project_id": "client-project",
        "group_id": "client-group",
        "user_id": "client-user",
    }
    source = model(
        **{key: value for key, value in client.items() if key in model.model_fields}
    )
    routing = {
        "base_url": "https://qa-model.sysevo.io/v1",
        "endpoint": "https://qa-resource.openai.azure.com/",
        "aws_region": "us-east-1",
        "region": "eastus",
        "location": "us-central1",
        "project_id": "qa-project",
        "group_id": "qa-group",
        "user_id": "qa-user",
    }
    secrets = {
        "api_key": QA_KEY,
        "credentials": google_credentials("qa-project"),
        "aws_access_key": "QA_ACCESS_KEY",
        "aws_secret_key": "QA_AWS_SECRET",
    }
    packet = sign(
        {
            "id": ID,
            "revision": 1,
            "scope": SCOPE,
            "service_type": service,
            "provider": provider,
            "schema_fingerprint": provider_schema_fingerprint(service, provider),
            "settings": {
                key: value
                for key, value in routing.items()
                if key in model.model_fields
            },
            "secrets": {
                key: value
                for key, value in secrets.items()
                if key in model.model_fields
            },
        }
    )
    return source, packet


def bindings(packets):
    return {
        packet["service_type"]: {
            key: packet[key] for key in ["id", "revision", "fingerprint"]
        }
        for packet in packets
    }


def plan_fixture(realtime=False, embeddings=False, signing_secret=SIGNING_SECRET):
    services = ["llm", "realtime"] if realtime else ["llm", "stt", "tts"]
    if embeddings:
        services.append("embeddings")
    sources, packets = {}, []
    for name in services:
        sources[name], packet = fixture(
            name, "openai_realtime" if name == "realtime" else "openai"
        )
        packets.append(sign(packet, signing_secret))
    source = UserConfiguration(**sources, is_realtime=realtime)
    plan = {
        "version": 1,
        "scope": SCOPE,
        "is_realtime": realtime,
        "timezone": source.timezone,
        "services": {
            name: {
                "provider": getattr(value.provider, "value", value.provider),
                **{
                    field: getattr(value, field)
                    for field in BEHAVIOR_FIELDS & set(type(value).model_fields)
                },
            }
            for name, value in sources.items()
        },
        "bindings": bindings(packets),
    }
    return source, packets, plan


@pytest.mark.parametrize(
    "realtime,embeddings", [(False, False), (False, True), (True, False), (True, True)]
)
def test_saved_behavior_plan_builds_only_qa_configuration_without_any_owner_lookup(
    realtime, embeddings
):
    from api.services.workflow.voice_test_providers import (
        private_voice_source_from_plan,
    )

    _, packets, plan = plan_fixture(realtime, embeddings)
    source = private_voice_source_from_plan(plan)
    result = build_private_user_configuration(
        source, packets, SIGNING_SECRET, plan["bindings"]
    )
    assert CLIENT_KEY not in result.model_dump_json()
    for name in plan["services"]:
        assert getattr(result, name).api_key == QA_KEY


@pytest.mark.parametrize(
    "bad",
    [
        "missing_behavior",
        "owner_secret",
        "owner_route",
        "missing_binding",
        "extra_service",
        "wrong_revision",
        "missing_timezone",
        "implicit_mode",
    ],
)
def test_saved_provider_plan_never_accepts_partial_behavior_or_client_connection_fields(
    bad,
):
    from api.services.workflow.voice_test_providers import (
        validate_private_voice_provider_plan,
    )

    _, _, plan = plan_fixture()
    if bad == "missing_behavior":
        plan["services"]["llm"].pop("model")
    elif bad == "owner_secret":
        plan["services"]["llm"]["api_key"] = CLIENT_KEY
    elif bad == "owner_route":
        plan["services"]["llm"]["base_url"] = "https://client.example.invalid/notify"
    elif bad == "missing_binding":
        plan["bindings"].pop("llm")
    elif bad == "extra_service":
        plan["services"]["email"] = {}
    elif bad == "wrong_revision":
        plan["bindings"]["llm"]["revision"] = True
    elif bad == "missing_timezone":
        plan.pop("timezone")
    else:
        plan["is_realtime"] = 0
    with pytest.raises(VoiceControlError):
        validate_private_voice_provider_plan(plan)


ALL_PROVIDERS = [
    (section, name) for section, kind in SECTIONS.items() for name in REGISTRY[kind]
]


@pytest.mark.parametrize("service,provider", ALL_PROVIDERS)
def test_every_registered_provider_preserves_behavior_and_replaces_client_connection_values(
    service, provider, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("provider config attempted network I/O"),
    )
    source, packet = fixture(service, provider)
    original = source.model_dump()
    result = build_private_provider_section(service, source, packet, SIGNING_SECRET)
    assert type(result) is type(source)
    for field in BEHAVIOR_FIELDS & set(type(source).model_fields):
        assert getattr(result, field) == getattr(source, field)
    for field in ROUTING_FIELDS & set(type(source).model_fields):
        assert getattr(result, field) == packet["settings"][field]
    serialized = json.dumps(result.model_dump(), default=str)
    assert CLIENT_KEY not in serialized
    assert "client-project" not in serialized
    assert "client-group" not in serialized
    assert "client-user" not in serialized
    assert "customer.example.invalid" not in serialized
    assert source.model_dump() == original
    assert set(
        type(result).model_fields
    ) <= BEHAVIOR_FIELDS | SECRET_FIELDS | ROUTING_FIELDS | {"provider"}


def test_registry_growth_requires_an_explicit_field_classification(monkeypatch):
    base = REGISTRY[ServiceType.LLM]["openai"]

    class ChangedConfiguration(base):
        unclassified_credential: str = CLIENT_KEY

    monkeypatch.setitem(REGISTRY[ServiceType.LLM], "openai", ChangedConfiguration)
    with pytest.raises(VoiceControlError, match="schema_unsupported"):
        provider_schema_fingerprint("llm", "openai")


@pytest.mark.parametrize(
    "patch",
    [
        {"id": ID.upper()},
        {"revision": 0},
        {"revision": True},
        {"revision": 1.5},
        {"scope": "production"},
        {"service_type": "webhook"},
        {"provider": "other"},
        {"schema_fingerprint": "f" * 64},
        {"settings": {"api_key": CLIENT_KEY}},
        {"secrets": {"base_url": "https://customer.invalid"}},
        {"settings": {"base_url": ""}},
        {"settings": {"base_url": "https://qa.sysevo.io/\n"}},
        {"secrets": {"api_key": "***masked"}},
        {"secrets": {"api_key": []}},
        {"secrets": {"api_key": ["same", "same"]}},
        {"secrets": {"api_key": ["qa", "***masked"]}},
        {"secrets": {"api_key": ["qa"] * 6}},
        {"secrets": {"api_key": "qa\r\nAuthorization: client"}},
        {"extra": "untrusted"},
    ],
)
def test_even_correctly_signed_packets_reject_invalid_scope_schema_and_secret_shapes(
    patch,
):
    _, packet = fixture()
    packet.update(patch)
    with pytest.raises(VoiceControlError) as error:
        ProviderConnection.parse(sign(packet), SIGNING_SECRET)
    assert CLIENT_KEY not in str(error.value)


def test_signature_covers_secret_settings_revision_and_identity():
    _, packet = fixture()
    for field, value in [
        ("secrets", {"api_key": CLIENT_KEY}),
        ("settings", {"base_url": "https://qa-other.sysevo.io"}),
        ("revision", 2),
        ("id", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    ]:
        changed = {**packet, field: value}
        with pytest.raises(VoiceControlError, match="signature_invalid"):
            ProviderConnection.parse(changed, SIGNING_SECRET)
    with pytest.raises(VoiceControlError, match="signature_invalid"):
        ProviderConnection.parse(packet, "wrong-signer")


def test_unsigned_constructor_objects_cannot_be_used_as_an_authenticated_packet():
    source, packet = fixture()
    connection = ProviderConnection.parse(packet, SIGNING_SECRET)
    assert QA_KEY not in repr(connection)
    with pytest.raises(VoiceControlError):
        build_private_provider_section("llm", source, connection, SIGNING_SECRET)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://customer.example.invalid/v1",
        "https://api.openai.com.evil.invalid/v1",
        "https://user:private@api.openai.com/v1",
        "https://api.openai.com/v1?token=private",
        "https://api.openai.com/v1#private",
        "https://api.openai.com:8443/v1",
        "https://api.openai.com./v1",
        "https://127.0.0.1/v1",
        "https://qa.sysevo.io.evil.invalid/v1",
        "file:///tmp/key",
    ],
)
def test_client_destinations_and_unsafe_provider_routes_are_rejected(url):
    source, packet = fixture()
    packet["settings"]["base_url"] = url
    with pytest.raises(VoiceControlError):
        build_private_provider_section("llm", source, sign(packet), SIGNING_SECRET)


def test_missing_qa_route_uses_the_registry_endpoint_and_never_the_client_override():
    source, packet = fixture()
    packet["settings"] = {}
    result = build_private_provider_section("llm", source, sign(packet), SIGNING_SECRET)
    assert result.base_url == type(source).model_fields["base_url"].default
    assert "customer" not in result.base_url


def test_qa_key_pools_are_copied_and_never_mutate_or_reuse_client_keys():
    source, packet = fixture()
    packet["secrets"]["api_key"] = ["qa-one", "qa-two"]
    packet = sign(packet)
    result = build_private_provider_section("llm", source, packet, SIGNING_SECRET)
    packet["secrets"]["api_key"].append(CLIENT_KEY)
    assert result.get_all_api_keys() == ["qa-one", "qa-two"]
    assert source.get_all_api_keys() == [CLIENT_KEY]


def test_missing_test_credentials_never_fall_back_to_the_owner_key():
    source, packet = fixture()
    packet["secrets"] = {}
    with pytest.raises(VoiceControlError, match="credentials_missing"):
        build_private_provider_section("llm", source, sign(packet), SIGNING_SECRET)


@pytest.mark.parametrize(
    "change",
    [
        {"token_uri": "https://customer.invalid/token"},
        {"universe_domain": "customer.invalid"},
        {"type": "external_account"},
        {"private_key": "***masked"},
    ],
)
def test_service_account_credentials_cannot_redirect_authentication_or_keep_masks(
    change,
):
    source, packet = fixture("tts", "google")
    credential = json.loads(packet["secrets"]["credentials"])
    credential.update(change)
    packet["secrets"]["credentials"] = json.dumps(credential)
    with pytest.raises(VoiceControlError):
        build_private_provider_section("tts", source, sign(packet), SIGNING_SECRET)


def test_private_voice_uri_must_not_reference_a_client_url():
    source, packet = fixture("tts", "openai")
    source.voice = "https://customer.invalid/voice.json"
    with pytest.raises(VoiceControlError, match="voice_reference_invalid"):
        build_private_provider_section("tts", source, packet, SIGNING_SECRET)


def test_standard_pipeline_has_only_qa_services_and_removes_the_client_phone_number():
    sections = {
        name: fixture(name, provider)
        for name, provider in [
            ("llm", "openai"),
            ("stt", "deepgram"),
            ("tts", "elevenlabs"),
            ("embeddings", "openai"),
        ]
    }
    source = UserConfiguration(
        **{name: pair[0] for name, pair in sections.items()},
        timezone="Europe/London",
        test_phone_number="+12025550199",
    )
    packets = [pair[1] for pair in sections.values()]
    result = build_private_user_configuration(
        source, packets, SIGNING_SECRET, bindings(packets)
    )
    assert result.timezone == source.timezone and result.test_phone_number is None
    assert source.test_phone_number == "+12025550199"
    assert result.realtime is None
    assert CLIENT_KEY not in result.model_dump_json()
    assert "client-project" not in result.model_dump_json()


def test_realtime_keeps_qa_out_of_band_inference_without_unused_owner_speech_services():
    llm, llm_packet = fixture()
    realtime, rt_packet = fixture("realtime", "openai_realtime")
    stt, _ = fixture("stt", "deepgram")
    tts, _ = fixture("tts", "openai")
    source = UserConfiguration(
        llm=llm, realtime=realtime, stt=stt, tts=tts, is_realtime=True
    )
    packets = [llm_packet, rt_packet]
    result = build_private_user_configuration(
        source, packets, SIGNING_SECRET, bindings(packets)
    )
    assert result.is_realtime and result.realtime is not None
    assert result.llm.get_all_api_keys() == [QA_KEY]
    assert result.stt is None and result.tts is None
    assert CLIENT_KEY not in result.model_dump_json()


def test_missing_extra_duplicate_or_mismatched_planned_connections_cannot_start_a_pipeline():
    pairs = {
        name: fixture(name, provider)
        for name, provider in [
            ("llm", "openai"),
            ("stt", "deepgram"),
            ("tts", "openai"),
        ]
    }
    source = UserConfiguration(**{name: pair[0] for name, pair in pairs.items()})
    packets = [pair[1] for pair in pairs.values()]
    for bad in [
        packets[:-1],
        packets + [packets[0]],
        packets + [fixture("embeddings", "openai")[1]],
    ]:
        with pytest.raises(VoiceControlError):
            build_private_user_configuration(
                source, bad, SIGNING_SECRET, bindings(packets)
            )
    expected = bindings(packets)
    expected["llm"]["revision"] = 2
    with pytest.raises(VoiceControlError, match="binding_invalid"):
        build_private_user_configuration(source, packets, SIGNING_SECRET, expected)
