from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from twilio.request_validator import RequestValidator

from api.services.telephony.providers.twilio.provider import TwilioProvider
from api.services.telephony.providers.twilio.routes import (
    handle_voice_number_inbound_status,
    handle_voice_number_inbound_sms,
    handle_inbound_voicemail_recording,
    handle_twilio_status_callback,
    handle_twiml_webhook,
)
from api.tasks.function_names import FunctionNames
import api.services.telephony.providers.twilio.provider as twilio_provider_module


def _provider() -> TwilioProvider:
    return TwilioProvider(
        {
            "account_sid": "AC123",
            "auth_token": "twilio-auth-token",
            "from_numbers": ["+15551230002"],
        }
    )


def _request(
    *,
    path: str,
    query: dict[str, str | int],
    form_data: dict[str, str],
    headers: dict[str, str] | None = None,
) -> Request:
    body = urlencode(form_data).encode("utf-8")
    query_string = urlencode(query).encode("utf-8")
    request_headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        *[
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in (headers or {}).items()
        ],
    ]

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("example.test", 443),
            "path": path,
            "query_string": query_string,
            "headers": request_headers,
        },
        receive,
    )


def _signature(
    provider: TwilioProvider,
    *,
    path: str,
    query: dict[str, str | int],
    form_data: dict[str, str],
) -> str:
    url = f"https://example.test{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    validator = RequestValidator(provider.auth_token)
    return validator.compute_signature(url, form_data)


def test_twilio_number_sms_callback_is_derived_from_voice_callback():
    voice_url = "https://api.example.test/api/v1/telephony/inbound/run"
    assert TwilioProvider._inbound_sms_callback(voice_url) == (
        "https://api.example.test/api/v1/telephony/twilio/inbound/sms"
    )
    assert TwilioProvider._inbound_sms_callback("https://custom.test/voice") is None
    assert TwilioProvider._is_voice_sms_callback(
        "https://api.example.test/api/v1/telephony/twilio/inbound/sms"
    )
    assert not TwilioProvider._is_voice_sms_callback("https://custom.test/inbound/sms")
    assert TwilioProvider._inbound_status_callback(voice_url) == (
        "https://api.example.test/api/v1/telephony/twilio/inbound/status-callback"
    )
    assert TwilioProvider._inbound_status_callback("https://custom.test/voice") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_sms_url", "should_update_sms", "existing_status_callback", "should_update_status"),
    [
        ("", True, "", True),
        ("https://customer.example/sms-hook", False, "https://customer.example/status", False),
        ("https://old.example/api/v1/telephony/twilio/inbound/sms", True,
         "https://old.example/api/v1/telephony/twilio/inbound/status-callback", True),
    ],
)
async def test_configure_inbound_preserves_custom_sms_urls(
    monkeypatch, existing_sms_url, should_update_sms, existing_status_callback, should_update_status
):
    provider = _provider()
    monkeypatch.setattr(provider, "_lookup_incoming_number_sid", AsyncMock(return_value="PN123"))
    posted = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"sms_url": existing_sms_url, "status_callback": existing_status_callback}

        async def text(self):
            return ""

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

        def post(self, _url, data, **_kwargs):
            posted.update(data)
            return Response()

    monkeypatch.setattr(twilio_provider_module.aiohttp, "ClientSession", Session)
    result = await provider.configure_inbound(
        "+15551230002",
        "https://api.example.test/api/v1/telephony/inbound/run",
    )

    assert result.ok
    assert ("SmsUrl" in posted) is should_update_sms
    if should_update_sms:
        assert posted["SmsUrl"] == "https://api.example.test/api/v1/telephony/twilio/inbound/sms"
    assert ("StatusCallback" in posted) is should_update_status
    if should_update_status:
        assert posted["StatusCallback"] == "https://api.example.test/api/v1/telephony/twilio/inbound/status-callback"
        assert posted["StatusCallbackMethod"] == "POST"


@pytest.mark.asyncio
async def test_inbound_status_callback_requires_signed_tenant_number_match():
    provider = _provider()
    provider.verify_inbound_signature = AsyncMock(return_value=True)
    config = SimpleNamespace(id=1, organization_id=77)
    phone = SimpleNamespace(id=88, inbound_workflow_id=99)
    run = SimpleNamespace(
        id=123, workflow_id=99,
        initial_context={"direction": "inbound", "caller_number": "+15551230001", "called_number": "+15551230002"},
    )
    payload = {
        "AccountSid": "AC" + "1" * 32, "CallSid": "CA" + "2" * 32,
        "To": "+15551230002", "From": "+15551230001", "Direction": "inbound", "CallStatus": "no-answer",
    }
    request = _request(path="/api/v1/telephony/twilio/inbound/status-callback", query={}, form_data=payload)
    with (
        patch("api.services.telephony.providers.twilio.routes.parse_webhook_request", new_callable=AsyncMock, return_value=(payload, "raw")),
        patch("api.services.telephony.providers.twilio.routes.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)) as route,
        patch("api.services.telephony.providers.twilio.routes.db_client.get_workflow_run_by_call_id", new_callable=AsyncMock, return_value=run),
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.telephony.providers.twilio.routes._process_status_update", new_callable=AsyncMock) as process,
    ):
        response = await handle_voice_number_inbound_status(request)
    assert response == {"status": "success"}
    route.assert_awaited_once_with(provider="twilio", account_id_field="account_sid", account_id=payload["AccountSid"], to_number=payload["To"])
    provider.verify_inbound_signature.assert_awaited_once()
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_status_without_a_run_queues_a_stable_missed_call_job():
    provider = _provider()
    provider.verify_inbound_signature = AsyncMock(return_value=True)
    config = SimpleNamespace(id=1, organization_id=77)
    phone = SimpleNamespace(id=88, inbound_workflow_id=99)
    payload = {
        "AccountSid": "AC" + "1" * 32, "CallSid": "CA" + "2" * 32,
        "To": "+15551230002", "From": "+15551230001", "Direction": "inbound", "CallStatus": "no-answer",
    }
    request = _request(path="/api/v1/telephony/twilio/inbound/status-callback", query={}, form_data=payload)
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    with (
        patch("api.services.telephony.providers.twilio.routes.parse_webhook_request", new_callable=AsyncMock, return_value=(payload, "raw")),
        patch("api.services.telephony.providers.twilio.routes.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)),
        patch("api.services.telephony.providers.twilio.routes.db_client.get_workflow_run_by_call_id", new_callable=AsyncMock, return_value=None),
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.telephony.providers.twilio.routes.get_arq_redis", new_callable=AsyncMock, return_value=redis),
    ):
        response = await handle_voice_number_inbound_status(request)
    assert response == {"status": "success"}
    redis.enqueue_job.assert_awaited_once_with(
        FunctionNames.PROCESS_MISSED_CALL_TEXT, 99, payload["CallSid"], 88,
        payload["From"], payload["To"], _job_id=f"missed-call-text:{payload['CallSid']}",
    )


@pytest.mark.asyncio
async def test_voice_number_sms_signature_and_tenant_route_gate_before_ingest():
    provider = _provider()
    provider.verify_inbound_signature = AsyncMock(return_value=False)
    config = SimpleNamespace(id=1, organization_id=77)
    phone = SimpleNamespace(id=88, inbound_workflow_id=99)
    payload = {
        "AccountSid": "AC" + "1" * 32,
        "MessageSid": "SM" + "2" * 32,
        "To": "+15551230002",
        "From": "+15551230001",
        "Body": "Hello",
    }
    request = _request(
        path="/api/v1/telephony/twilio/inbound/sms",
        query={},
        form_data=payload,
    )
    with (
        patch("api.services.telephony.providers.twilio.routes.parse_webhook_request", new_callable=AsyncMock, return_value=(payload, "raw")),
        patch("api.services.telephony.providers.twilio.routes.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)),
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.phone_tools.builtin.ingest_inbound_voice_text", new_callable=AsyncMock) as ingest,
    ):
        response = await handle_voice_number_inbound_sms(request)

    assert response.status_code == 403
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_number_sms_enqueues_only_the_verified_tenant_agent_reply():
    provider = _provider()
    provider.verify_inbound_signature = AsyncMock(return_value=True)
    config = SimpleNamespace(id=1, organization_id=77)
    phone = SimpleNamespace(id=88, inbound_workflow_id=99)
    payload = {
        "AccountSid": "AC" + "1" * 32,
        "MessageSid": "SM" + "2" * 32,
        "To": "+15551230002",
        "From": "+15551230001",
        "Body": "Hello",
    }
    request = _request(
        path="/api/v1/telephony/twilio/inbound/sms",
        query={},
        form_data=payload,
    )
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    with (
        patch("api.services.telephony.providers.twilio.routes.parse_webhook_request", new_callable=AsyncMock, return_value=(payload, "raw")),
        patch("api.services.telephony.providers.twilio.routes.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)) as route,
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.phone_tools.builtin.ingest_inbound_voice_text", new_callable=AsyncMock, return_value={"status": "received", "should_reply": True, "reply": None}),
        patch("api.services.telephony.providers.twilio.routes.get_arq_redis", new_callable=AsyncMock, return_value=redis),
    ):
        response = await handle_voice_number_inbound_sms(request)

    assert response.status_code == 200
    route.assert_awaited_once_with(
        provider="twilio", account_id_field="account_sid",
        account_id=payload["AccountSid"], to_number=payload["To"],
    )
    redis.enqueue_job.assert_awaited_once_with(
        "process_inbound_voice_sms", 99, 77, 88, payload["To"], payload["From"],
        payload["Body"], payload["MessageSid"],
        _job_id=f"voice-sms:{payload['MessageSid']}",
    )


@pytest.mark.asyncio
async def test_number_policy_twiml_forwards_only_e164_and_records_to_callback():
    provider = _provider()
    forward = await provider.inbound_number_policy_response(action="fwd", forward_to="+15551230003")
    assert b'<Dial timeout="30"><Number>+15551230003</Number></Dial>' in forward.body
    invalid = await provider.inbound_number_policy_response(action="fwd", forward_to="15551230003")
    assert invalid is None
    voicemail = await provider.inbound_number_policy_response(action="vm", voicemail_run_id=123, backend_endpoint="https://example.test")
    assert b'<Record' in voicemail.body
    assert b'action="https://example.test/api/v1/telephony/twilio/inbound-voicemail/123"' in voicemail.body


@pytest.mark.asyncio
async def test_twiml_route_accepts_valid_signature_with_extra_query_param():
    provider = _provider()
    query = {
        "workflow_id": 7,
        "user_id": 8,
        "workflow_run_id": 123,
        "campaign_id": 42,
        "organization_id": 11,
    }
    form_data = {"CallSid": "CA123", "CallStatus": "in-progress"}
    request = _request(
        path="/api/v1/telephony/twiml",
        query=query,
        form_data=form_data,
        headers={
            "x-twilio-signature": _signature(
                provider,
                path="/api/v1/telephony/twiml",
                query=query,
                form_data=form_data,
            )
        },
    )

    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch(
            "api.services.telephony.providers.twilio.routes.get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=provider,
        ),
        patch.object(
            provider,
            "get_webhook_response",
            new_callable=AsyncMock,
            return_value="<Response/>",
        ) as get_webhook_response,
    ):
        db_client.get_workflow_run_by_id = AsyncMock(
            return_value=SimpleNamespace(id=123)
        )

        response = await handle_twiml_webhook(
            workflow_id=7,
            user_id=8,
            workflow_run_id=123,
            organization_id=11,
            request=request,
        )

    assert response.body == b"<Response/>"
    get_webhook_response.assert_awaited_once_with(7, 8, 123)


@pytest.mark.asyncio
async def test_twiml_route_rejects_missing_signature():
    provider = _provider()
    request = _request(
        path="/api/v1/telephony/twiml",
        query={
            "workflow_id": 7,
            "user_id": 8,
            "workflow_run_id": 123,
            "organization_id": 11,
        },
        form_data={"CallSid": "CA123"},
    )

    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch(
            "api.services.telephony.providers.twilio.routes.get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=provider,
        ),
    ):
        db_client.get_workflow_run_by_id = AsyncMock(
            return_value=SimpleNamespace(id=123)
        )

        response = await handle_twiml_webhook(
            workflow_id=7,
            user_id=8,
            workflow_run_id=123,
            organization_id=11,
            request=request,
        )

    assert response.status_code == 200
    assert b"<Hangup/>" in response.body


@pytest.mark.asyncio
async def test_twilio_status_callback_rejects_legacy_header_name():
    provider = _provider()
    form_data = {"CallSid": "CA123", "CallStatus": "completed"}
    request = _request(
        path="/api/v1/telephony/twilio/status-callback/123",
        query={},
        form_data=form_data,
        headers={"x-webhook-signature": "not-a-twilio-signature"},
    )

    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch(
            "api.services.telephony.providers.twilio.routes.get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=provider,
        ),
        patch(
            "api.services.telephony.providers.twilio.routes._process_status_update",
            new_callable=AsyncMock,
        ) as process_status,
    ):
        db_client.get_workflow_run_by_id = AsyncMock(
            return_value=SimpleNamespace(workflow_id=7)
        )
        db_client.get_workflow_by_id = AsyncMock(
            return_value=SimpleNamespace(organization_id=11)
        )

        with pytest.raises(HTTPException) as exc_info:
            await handle_twilio_status_callback(workflow_run_id=123, request=request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid webhook signature"
    process_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_twilio_status_callback_accepts_valid_signature():
    provider = _provider()
    form_data = {"CallSid": "CA123", "CallStatus": "completed"}
    request = _request(
        path="/api/v1/telephony/twilio/status-callback/123",
        query={},
        form_data=form_data,
        headers={
            "x-twilio-signature": _signature(
                provider,
                path="/api/v1/telephony/twilio/status-callback/123",
                query={},
                form_data=form_data,
            )
        },
    )

    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch(
            "api.services.telephony.providers.twilio.routes.get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=provider,
        ),
        patch(
            "api.services.telephony.providers.twilio.routes._process_status_update",
            new_callable=AsyncMock,
        ) as process_status,
    ):
        db_client.get_workflow_run_by_id = AsyncMock(
            return_value=SimpleNamespace(workflow_id=7)
        )
        db_client.get_workflow_by_id = AsyncMock(
            return_value=SimpleNamespace(organization_id=11)
        )

        result = await handle_twilio_status_callback(
            workflow_run_id=123, request=request
        )

    assert result == {"status": "success"}
    process_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_voicemail_callback_records_no_message_without_fetching_provider_media():
    provider = _provider()
    path = "/api/v1/telephony/twilio/inbound-voicemail/123"
    form_data = {
        "AccountSid": "AC123", "CallSid": "CA123",
        "From": "+15551230001", "To": "+15551230002",
        "RecordingDuration": "0",
    }
    request = _request(path=path, query={}, form_data=form_data, headers={
        "x-twilio-signature": _signature(provider, path=path, query={}, form_data=form_data),
    })
    run = SimpleNamespace(
        id=123, workflow_id=7, storage_backend="s3",
        annotations={"voice_number_voicemail": {"enabled": True, "organization_id": 11}},
        initial_context={"telephony_configuration_id": 44, "caller_number": "+15551230001", "called_number": "+15551230002"},
        gathered_context={"call_id": "CA123"},
    )
    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
    ):
        db_client.get_workflow_run_by_id = AsyncMock(return_value=run)
        db_client.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(organization_id=11))
        db_client.update_workflow_run = AsyncMock()
        db_client.mark_wallet_debit_settled = AsyncMock()
        db_client.mark_memory_settled = AsyncMock()

        response = await handle_inbound_voicemail_recording(123, request)

    assert response.status_code == 200
    update = db_client.update_workflow_run.await_args.kwargs
    assert update["is_completed"] is True
    assert update["recording_url"] is None
    assert update["gathered_context"]["mapped_call_disposition"] == "no_answer"
    db_client.mark_wallet_debit_settled.assert_awaited_once_with(123)
    db_client.mark_memory_settled.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_inbound_voicemail_callback_rejects_bad_signature_before_finalizing():
    provider = _provider()
    path = "/api/v1/telephony/twilio/inbound-voicemail/123"
    form_data = {"AccountSid": "AC123", "CallSid": "CA123"}
    request = _request(path=path, query={}, form_data=form_data)
    run = SimpleNamespace(
        id=123, workflow_id=7,
        annotations={"voice_number_voicemail": {"enabled": True, "organization_id": 11}},
        initial_context={"telephony_configuration_id": 44}, gathered_context={"call_id": "CA123"},
    )
    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
    ):
        db_client.get_workflow_run_by_id = AsyncMock(return_value=run)
        db_client.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(organization_id=11))
        db_client.update_workflow_run = AsyncMock()

        response = await handle_inbound_voicemail_recording(123, request)

    assert response.status_code == 401
    db_client.update_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_voicemail_callback_downloads_authenticated_media_and_saves_run_recording():
    provider = _provider()
    path = "/api/v1/telephony/twilio/inbound-voicemail/123"
    form_data = {
        "AccountSid": "AC123", "CallSid": "CA123",
        "From": "+15551230001", "To": "+15551230002",
        "RecordingSid": "RE" + "A" * 32, "RecordingDuration": "17",
    }
    request = _request(path=path, query={}, form_data=form_data, headers={
        "x-twilio-signature": _signature(provider, path=path, query={}, form_data=form_data),
    })
    run = SimpleNamespace(
        id=123, workflow_id=7, storage_backend="s3",
        annotations={"voice_number_voicemail": {"enabled": True, "organization_id": 11}},
        initial_context={"telephony_configuration_id": 44, "caller_number": "+15551230001", "called_number": "+15551230002"},
        gathered_context={"call_id": "CA123"},
    )
    stored = []

    class MediaResponse:
        status_code = 200
        content = b"RIFF" + b"\x00" * 64

    class HttpClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, url, auth):
            assert url.endswith("/Recordings/" + form_data["RecordingSid"] + ".wav")
            assert auth == ("AC123", "twilio-auth-token")
            return MediaResponse()

    class Storage:
        async def aupload_file(self, local_path, key):
            stored.append(key)
            return True

    with (
        patch("api.services.telephony.providers.twilio.routes.db_client") as db_client,
        patch("api.services.telephony.providers.twilio.routes.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.telephony.providers.twilio.routes.httpx.AsyncClient", side_effect=lambda **_: HttpClient()),
        patch("api.services.storage.get_storage_for_backend", return_value=Storage()),
    ):
        db_client.get_workflow_run_by_id = AsyncMock(return_value=run)
        db_client.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(organization_id=11))
        db_client.update_workflow_run = AsyncMock()
        db_client.mark_wallet_debit_settled = AsyncMock()
        db_client.mark_memory_settled = AsyncMock()

        response = await handle_inbound_voicemail_recording(123, request)

    assert response.status_code == 200
    assert stored == ["recordings/123.wav"]
    update = db_client.update_workflow_run.await_args.kwargs
    assert update["recording_url"] == "recordings/123.wav"
    assert update["gathered_context"]["mapped_call_disposition"] == "voicemail"
    assert update["cost_info"]["call_duration_seconds"] == 17
