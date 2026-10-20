"""Run real factory selection/settings up to, but never inside, SDK constructors.

This proves credential forwarding, not provider authentication or audio quality.
Network access is forbidden. Explicitly unsupported native adapters stay blocked.
"""

import ast
import importlib
import inspect
import json
import socket
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services.workflow.voice_test_providers import build_private_user_configuration
from api.tests.test_voice_test_providers import (
    ALL_PROVIDERS,
    CLIENT_KEY,
    SIGNING_SECRET,
    bindings,
    fixture,
    plan_fixture,
)


class SDKBoundary(BaseException):
    def __init__(self, name, args, kwargs):
        self.name, self.args_received, self.kwargs_received = name, args, kwargs


def stop(name):
    def constructor(self, *args, **kwargs):
        raise SDKBoundary(name, args, kwargs)

    return constructor


@pytest.mark.parametrize("service,provider", ALL_PROVIDERS)
@pytest.mark.asyncio
async def test_actual_factory_forwards_only_explicit_qa_credentials(
    monkeypatch, service, provider
):
    def network_forbidden(*args, **kwargs):
        raise AssertionError("An SDK boundary test must not use a network")

    monkeypatch.setattr(socket, "getaddrinfo", network_forbidden)
    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    from api.services.pipecat import service_factory as factory
    from api.services.pipecat.audio_config import create_audio_config

    # The URL guard's DNS behavior has its own tests. Here, its exact input must
    # be a pinned QA route; resolving it would turn this into a network test.
    def validate_route(url, field):
        assert "customer.example" not in url and "client-resource" not in url

    monkeypatch.setattr(factory, "_validate_runtime_service_url", validate_route)
    for name, value in list(vars(factory).items()):
        if name.endswith("Service") and inspect.isclass(value):
            monkeypatch.setattr(value, "__init__", stop(name))
    # Preserve each SDK class's Settings/InputParams types, including lazy
    # provider imports. Replacing only __init__ exercises actual factory args.
    for node in ast.walk(ast.parse(inspect.getsource(factory))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [
                alias.name for alias in node.names if alias.name.endswith("Service")
            ]
            if names:
                module = importlib.import_module(node.module)
                for name in names:
                    cls = getattr(module, name)
                    if inspect.isclass(cls):
                        monkeypatch.setattr(cls, "__init__", stop(name))

    source, packets, _ = plan_fixture(
        realtime=service == "realtime", embeddings=service == "embeddings"
    )
    section, packet = fixture(service, provider)
    setattr(source, service, section)
    packets = [item for item in packets if item["service_type"] != service] + [packet]
    private = build_private_user_configuration(
        source, packets, SIGNING_SECRET, bindings(packets)
    )
    audio = create_audio_config("smallwebrtc")
    if service == "embeddings":
        from api.services.gen_ai.embedding import openai_service
        from api.services.workflow.voice_test_providers import (
            private_voice_embedding_configuration,
        )

        monkeypatch.setattr(
            openai_service,
            "validate_user_configured_service_url",
            lambda url, **kwargs: validate_route(url, "base_url"),
        )
        monkeypatch.setattr(openai_service.AsyncOpenAI, "__init__", stop("AsyncOpenAI"))
        key, model, url = private_voice_embedding_configuration(private)
        call = lambda: openai_service.OpenAIEmbeddingService(
            SimpleNamespace(), api_key=key, model_id=model, base_url=url
        )
    else:
        call = {
            "llm": lambda: factory.create_llm_service(private),
            "stt": lambda: factory.create_stt_service(private, audio),
            "tts": lambda: factory.create_tts_service(private, audio),
            "realtime": lambda: factory.create_realtime_llm_service(private, audio),
        }[service]
    if service == "tts" and provider in {"playht", "neets"}:
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 501
        return
    with pytest.raises(SDKBoundary) as boundary:
        call()
    session = boundary.value.kwargs_received.get("aiohttp_session")
    if session is not None:
        await session.close()
    values = repr((boundary.value.args_received, boundary.value.kwargs_received))
    assert CLIENT_KEY not in values
    assert (
        "CLIENT_ACCESS_KEY" not in values
        and "client-project" not in values
        and "customer.example" not in values
    )
    for key, secret in packet["secrets"].items():
        if (
            key == "api_key"
            and getattr(getattr(private, service), "api_key", None) is None
        ):
            continue  # Cloud providers explicitly use their other credential fields.
        if key == "credentials":
            assert json.loads(secret)["private_key"] in values
        else:
            assert secret in values
