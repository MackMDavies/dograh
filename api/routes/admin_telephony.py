"""
Superuser endpoints for platform-level managed telephony.
"""
import asyncio
import os
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client
from api.db.models import (
    OrganizationModel,
    TelephonyConfigurationModel,
    TelephonyPhoneNumberModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.auth.depends import get_superuser

router = APIRouter(prefix="/admin/telephony", tags=["admin-telephony"])

# Approx Twilio monthly rental for a local number, in USD cents — standard
# published rates (admins can confirm exact amounts in the Twilio console).
_NUMBER_MONTHLY_COST_CENTS = {
    "US": 115, "CA": 115, "PR": 115,
    "GB": 115, "IE": 115,
    "AU": 600, "NZ": 600,
    "FR": 150, "DE": 150, "NL": 150, "ES": 150, "IT": 150, "BE": 150,
    "CH": 300, "AT": 150, "SE": 150, "NO": 300, "DK": 150, "FI": 150,
    "PT": 150, "PL": 150,
}
_DEFAULT_MONTHLY_COST_CENTS = 150


class ManagedStatusResponse(BaseModel):
    configured: bool
    account_sid_preview: Optional[str]  # e.g. "AC12ab****" or None
    source: Optional[Literal["database", "environment"]] = None


class PlatformTwilioAccountItem(BaseModel):
    id: int
    label: Optional[str]
    account_sid_preview: str
    is_active: bool
    last_validated_at: Optional[str]
    created_at: Optional[str]
    dialer_configured: bool
    # Identifiers, not secrets — included so the edit form can pre-fill them.
    dialer_api_key_sid: Optional[str] = None
    dialer_twiml_app_sid: Optional[str] = None
    dialer_default_caller_id: Optional[str] = None


class PlatformTwilioAccountsResponse(BaseModel):
    accounts: list[PlatformTwilioAccountItem]
    env_fallback_configured: bool  # SYSEVO_TWILIO_* set, used only if no row is active


class AddTwilioAccountRequest(BaseModel):
    account_sid: str
    auth_token: str
    label: Optional[str] = None
    make_active: bool = False
    # Optional — see PlatformTwilioCredentialsModel docstring. Only needed if
    # this account should also power the internal browser dialer.
    dialer_api_key_sid: Optional[str] = None
    dialer_api_key_secret: Optional[str] = None
    dialer_twiml_app_sid: Optional[str] = None
    dialer_default_caller_id: Optional[str] = None


class AddTwilioAccountResponse(BaseModel):
    id: int
    friendly_name: Optional[str] = None


class UpdateTwilioAccountRequest(BaseModel):
    """
    All fields optional — only fields the client actually sets are applied
    (see ``BaseModel.model_dump(exclude_unset=True)`` in the handler), so a
    partial edit (e.g. "just fix the TwiML App SID") never touches anything
    else. Send "" to clear a nullable field; omit a field entirely to leave
    it untouched. account_sid/auth_token are re-validated against Twilio only
    when both are present in the same request (changing just one would leave
    a mismatched pair, so we require them together).
    """
    label: Optional[str] = None
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    dialer_api_key_sid: Optional[str] = None
    dialer_api_key_secret: Optional[str] = None
    dialer_twiml_app_sid: Optional[str] = None
    dialer_default_caller_id: Optional[str] = None


class ManagedNumberItem(BaseModel):
    phone_number_id: int
    address: str
    country_code: Optional[str]
    label: Optional[str]
    organization_id: int
    organization_name: str  # provider_id used as display name
    inbound_workflow_id: Optional[int]
    inbound_workflow_name: Optional[str]
    twilio_sid_preview: Optional[str]  # first 6 chars + "****"
    is_active: bool
    created_at: Optional[str]
    monthly_cost_cents: int  # estimated Twilio rental cost (USD cents)
    call_count: int          # calls run through the number (runs of its inbound workflow)
    # Which platform Twilio account this number was bought under — label if the
    # account still has one and exists, else a masked SID, else None (the
    # number's org-level config has no readable account_sid, e.g. legacy row).
    platform_account_label: Optional[str] = None
    platform_account_sid_preview: Optional[str] = None


class ManagedNumbersResponse(BaseModel):
    numbers: list[ManagedNumberItem]
    total: int
    total_monthly_cost_cents: int


class TelnyxDialerCredentialsRequest(BaseModel):
    api_key: Optional[str] = None
    connection_id: str


class TelnyxDialerCredentialsResponse(BaseModel):
    configured: bool
    api_key_preview: Optional[str] = None
    connection_id: Optional[str] = None
    telephony_credential_id: Optional[str] = None
    updated_at: Optional[str] = None


class VonageDialerCredentialsRequest(BaseModel):
    application_id: str
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    private_key: Optional[str] = None
    signature_secret: Optional[str] = None


class VonageDialerCredentialsResponse(BaseModel):
    configured: bool
    purchasing_configured: bool = False
    application_id: Optional[str] = None
    api_key_preview: Optional[str] = None
    updated_at: Optional[str] = None


class DialerNumberSearchRequest(BaseModel):
    provider: Literal["twilio", "vonage"] = "twilio"
    country_code: str
    number_type: Literal["local", "toll-free", "mobile"] = "local"
    area_code: Optional[str] = None
    contains: Optional[str] = None


class DialerNumberPurchaseRequest(DialerNumberSearchRequest):
    phone_number: str


def _mask_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return f"{value[:5]}…{value[-4:]}" if len(value) > 12 else "••••••••"


async def _active_twilio_dialer_client() -> Client:
    """Resolve the active platform Twilio account used by the browser dialer."""
    credentials = await db_client.get_platform_twilio_credentials()
    if credentials:
        return Client(credentials["account_sid"], credentials["auth_token"])
    account_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise HTTPException(status_code=503, detail="No active platform Twilio account is configured.")
    return Client(account_sid, auth_token)


async def _telnyx_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        logger.error(f"Telnyx API request failed: {type(exc).__name__}")
        raise HTTPException(status_code=502, detail="Could not reach Telnyx. Check the connection and try again.") from exc


async def _telnyx_dialer_credentials() -> dict:
    saved = await db_client.get_platform_telnyx_dialer_credentials()
    if saved:
        return saved
    return {
        "api_key": os.environ.get("TELNYX_API_KEY", ""),
        "connection_id": os.environ.get("TELNYX_DIALER_CONNECTION_ID", ""),
        "telephony_credential_id": os.environ.get("TELNYX_DIALER_CREDENTIAL_ID", ""),
    }


@router.get("/dialer-providers")
async def get_dialer_provider_status(_user: UserModel = Depends(get_superuser)):
    """Report readiness for rep-dialer carriers, apart from AI voice configs."""
    from api.services.telephony.dialer.provider import resolve_active_dialer_provider

    active_provider = resolve_active_dialer_provider()
    twilio_accounts = await db_client.list_platform_twilio_accounts()
    twilio_dialer = await db_client.get_platform_dialer_credentials()
    twilio_env = all(os.environ.get(key) for key in (
        "SYSEVO_TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET", "TWILIO_TWIML_APP_SID",
    ))
    telnyx = await _telnyx_dialer_credentials()
    try:
        vonage = await db_client.get_platform_vonage_dialer_credentials()
    except Exception:
        vonage = None
    vonage = vonage or {}
    vonage_env = all(os.environ.get(key) for key in (
        "VONAGE_DIALER_APPLICATION_ID", "VONAGE_DIALER_API_KEY",
        "VONAGE_DIALER_PRIVATE_KEY", "VONAGE_DIALER_SIGNATURE_SECRET",
    ))
    signalwire_missing = [
        label for env, label in (
            ("SIGNALWIRE_SPACE_URL", "space URL"),
            ("SIGNALWIRE_PROJECT_ID", "project ID"),
            ("SIGNALWIRE_API_TOKEN", "API token"),
        ) if not os.environ.get(env)
    ]
    return {
        "active_provider": active_provider,
        "signalwire": {
            "configured": not signalwire_missing,
            "source": "environment",
            "missing": signalwire_missing,
        },
        "telnyx": {
            "configured": bool(telnyx.get("api_key") and (telnyx.get("connection_id") or telnyx.get("telephony_credential_id"))),
            "source": "database" if await db_client.get_platform_telnyx_dialer_credentials() else "environment",
        },
        "twilio": {
            "configured": bool(twilio_dialer or twilio_env),
            "source": "database" if twilio_dialer else ("environment" if twilio_env else None),
            "account_count": len(twilio_accounts),
            "active_dialer_configured": bool(twilio_dialer or twilio_env),
        },
        "vonage": {
            "configured": bool(
                (vonage.get("application_id") and vonage.get("api_key")
                 and vonage.get("private_key") and vonage.get("signature_secret"))
                or vonage_env
            ),
            "source": "database" if vonage else ("environment" if vonage_env else None),
        },
    }


def _telnyx_credentials_response(saved: Optional[dict]) -> TelnyxDialerCredentialsResponse:
    saved = saved or {}
    key = saved.get("api_key") or ""
    return TelnyxDialerCredentialsResponse(
        configured=bool(
            key and saved.get("connection_id") and saved.get("telephony_credential_id")
        ),
        api_key_preview=_mask_key(key),
        connection_id=saved.get("connection_id") or None,
        telephony_credential_id=saved.get("telephony_credential_id") or None,
        updated_at=(saved.get("updated_at").isoformat() if saved.get("updated_at") else None),
    )


def _vonage_credentials_response(saved: Optional[dict]) -> VonageDialerCredentialsResponse:
    saved = saved or {}
    api_key = saved.get("api_key") or ""
    return VonageDialerCredentialsResponse(
        configured=bool(
            saved.get("application_id") and api_key
            and saved.get("private_key") and saved.get("signature_secret")
        ),
        purchasing_configured=bool(api_key and saved.get("api_secret")),
        application_id=saved.get("application_id") or None,
        api_key_preview=_mask_key(api_key),
        updated_at=saved.get("updated_at").isoformat() if saved.get("updated_at") else None,
    )


@router.get("/vonage-dialer", response_model=VonageDialerCredentialsResponse)
async def get_vonage_dialer_settings(_user: UserModel = Depends(get_superuser)):
    saved = await db_client.get_platform_vonage_dialer_credentials()
    return _vonage_credentials_response(saved)


@router.put("/vonage-dialer", response_model=VonageDialerCredentialsResponse)
async def save_vonage_dialer_settings(
    body: VonageDialerCredentialsRequest,
    _user: UserModel = Depends(get_superuser),
):
    current = await db_client.get_platform_vonage_dialer_credentials() or {}
    application_id = body.application_id.strip()
    api_key = (body.api_key or current.get("api_key") or "").strip()
    api_secret = (body.api_secret or current.get("api_secret") or "").strip() or None
    private_key = (body.private_key or current.get("private_key") or "").replace("\\n", "\n").strip()
    signature_secret = (body.signature_secret or current.get("signature_secret") or "").strip()
    if not application_id or not api_key or not private_key or not signature_secret:
        raise HTTPException(status_code=422, detail="Application ID, API key, private key, and signature secret are required.")
    if len(signature_secret) < 32:
        raise HTTPException(status_code=422, detail="Use the Vonage webhook signature secret (at least 32 characters).")
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Vonage requires an RSA private key.")
        key.sign(b"sysevo-vonage-dialer-check", padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001 - don't persist unusable signing keys
        raise HTTPException(status_code=422, detail="The Vonage private key must be a valid RSA PEM key.") from exc
    await db_client.save_platform_vonage_dialer_credentials(
        application_id=application_id,
        api_key=api_key,
        api_secret=api_secret,
        private_key=private_key,
        signature_secret=signature_secret,
    )
    return _vonage_credentials_response(await db_client.get_platform_vonage_dialer_credentials())


def _number_type_resource(client: Client, country_code: str, number_type: str):
    country = client.available_phone_numbers(country_code.upper())
    return {
        "toll-free": country.toll_free,
        "mobile": country.mobile,
    }.get(number_type, country.local)


async def _sync_dialer_number(*, provider: str, phone_number: str, friendly_name: Optional[str] = None, sid: Optional[str] = None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Dialer number inventory sync is not configured.")
    number_row = {"provider": provider, "phone_number": phone_number}
    if friendly_name:
        number_row["friendly_name"] = friendly_name
    if sid or provider != "twilio":
        number_row["twilio_sid"] = sid
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/dialer_phone_numbers?on_conflict=phone_number",
            json=number_row,
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
    if not response.is_success:
        logger.error(f"Dialer number inventory sync failed ({response.status_code}): {response.text[:250]}")
        raise HTTPException(status_code=502, detail="Carrier number succeeded, but its dialer inventory sync failed.")


@router.post("/dialer-numbers/search")
async def search_dialer_numbers(body: DialerNumberSearchRequest, _user: UserModel = Depends(get_superuser)):
    # Both browser adapters are selected from the rep's assigned number, so
    # admins can manage an account without switching the global fallback.
    provider = body.provider
    country = body.country_code.strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise HTTPException(status_code=422, detail="country_code must be a two-letter ISO country code.")
    area_code = "".join(c for c in (body.area_code or "") if c.isdigit())[:6]
    contains = "".join(c for c in (body.contains or "") if c.isdigit())[:8]
    if provider == "twilio":
        client = await _active_twilio_dialer_client()
        params = {"voice_enabled": True, "limit": 20}
        if area_code: params["area_code"] = area_code
        if contains: params["contains"] = contains
        try:
            available = await asyncio.to_thread(lambda: _number_type_resource(client, country, body.number_type).list(**params))
        except TwilioRestException as exc:
            raise HTTPException(status_code=400, detail=f"Twilio number search failed: {exc.msg}") from exc
        offers = [{"phone_number": item.phone_number, "country_code": country, "number_type": body.number_type,
                   "capabilities": [name for name, enabled in (item.capabilities or {}).items() if enabled], "monthly_cost": None, "currency": None} for item in available]
        return {"provider": provider, "offers": offers}

    credentials = await db_client.get_platform_vonage_dialer_credentials()
    if not credentials or not credentials.get("api_secret"):
        raise HTTPException(status_code=503, detail="Save the Vonage account API secret to search and purchase dialer numbers.")
    params = {"country": country, "features": "VOICE", "size": 20,
              "type": {"local": "landline", "toll-free": "landline-toll-free", "mobile": "mobile-lvn"}[body.number_type]}
    pattern = area_code or contains
    if pattern:
        params["pattern"] = pattern
        # Vonage patterns are matched against E.164 digits including the
        # country prefix; use contains for national area codes to avoid
        # incorrectly requiring the pattern to start before that prefix.
        params["search_pattern"] = "1"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://rest.nexmo.com/number/search", params=params, auth=(credentials["api_key"], credentials["api_secret"]))
    result = response.json()
    if not response.is_success:
        raise HTTPException(status_code=400, detail=str(result.get("error-code-label") or "Vonage number search failed."))
    offers = [{"phone_number": f"+{item['msisdn']}", "country_code": item.get("country", country),
               "number_type": body.number_type, "capabilities": item.get("features", []),
               "monthly_cost": item.get("cost"), "currency": "EUR"} for item in result.get("numbers", [])]
    return {"provider": provider, "offers": offers}


@router.post("/dialer-numbers/purchase")
async def purchase_dialer_number(body: DialerNumberPurchaseRequest, _user: UserModel = Depends(get_superuser)):
    provider = body.provider
    if not body.phone_number.startswith("+") or not body.phone_number[1:].isdigit():
        raise HTTPException(status_code=422, detail="phone_number must be E.164 formatted.")
    # Re-run the exact same search immediately before the paid action.
    offers = await search_dialer_numbers(body, _user)
    if not any(item["phone_number"] == body.phone_number for item in offers["offers"]):
        raise HTTPException(status_code=409, detail="That number is no longer available. Search again before purchasing.")
    if provider == "twilio":
        client = await _active_twilio_dialer_client()
        try:
            purchased = await asyncio.to_thread(lambda: client.incoming_phone_numbers.create(phone_number=body.phone_number))
        except TwilioRestException as exc:
            raise HTTPException(status_code=400, detail=f"Twilio could not purchase this number: {exc.msg}") from exc
        await _sync_dialer_number(provider=provider, phone_number=purchased.phone_number, friendly_name=purchased.friendly_name, sid=purchased.sid)
        return {"purchased": {"phone_number": purchased.phone_number, "provider": provider, "status": "active"}, "sync_required": False}

    credentials = await db_client.get_platform_vonage_dialer_credentials()
    if not credentials or not credentials.get("api_secret"):
        raise HTTPException(status_code=503, detail="Save the Vonage account API secret to purchase dialer numbers.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://rest.nexmo.com/number/buy", data={"country": body.country_code.upper(), "msisdn": body.phone_number.lstrip("+")}, auth=(credentials["api_key"], credentials["api_secret"]))
    result = response.json()
    if not response.is_success or str(result.get("error-code")) != "200":
        raise HTTPException(status_code=400, detail=str(result.get("error-code-label") or "Vonage could not purchase this number."))
    await _sync_dialer_number(provider=provider, phone_number=body.phone_number, friendly_name="Vonage dialer number")
    return {"purchased": {"phone_number": body.phone_number, "provider": provider, "status": "active"}, "sync_required": False}


@router.post("/vonage-dialer/sync-numbers")
async def sync_vonage_dialer_numbers(_user: UserModel = Depends(get_superuser)):
    credentials = await db_client.get_platform_vonage_dialer_credentials()
    if not credentials or not credentials.get("api_secret"):
        raise HTTPException(status_code=503, detail="Save the Vonage account API secret before syncing numbers.")
    numbers = []
    async with httpx.AsyncClient(timeout=15) as client:
        for page in range(1, 101):
            response = await client.get(
                "https://rest.nexmo.com/account/numbers",
                params={"size": 100, "index": page},
                auth=(credentials["api_key"], credentials["api_secret"]),
            )
            result = response.json()
            if not response.is_success:
                raise HTTPException(status_code=502, detail="Vonage could not list owned numbers.")
            numbers.extend(result.get("numbers", []))
            if len(numbers) >= int(result.get("count", len(numbers))) or not result.get("numbers"):
                break
    for number in numbers:
        if "VOICE" in str(number.get("features", "")).upper():
            await _sync_dialer_number(provider="vonage", phone_number=f"+{number['msisdn']}", friendly_name="Vonage dialer number")
    return {"synced": sum("VOICE" in str(number.get("features", "")).upper() for number in numbers)}


@router.get("/telnyx-dialer", response_model=TelnyxDialerCredentialsResponse)
async def get_telnyx_dialer_settings(_user: UserModel = Depends(get_superuser)):
    saved = await _telnyx_dialer_credentials()
    return _telnyx_credentials_response(saved)


@router.put("/telnyx-dialer", response_model=TelnyxDialerCredentialsResponse)
async def save_telnyx_dialer_settings(
    body: TelnyxDialerCredentialsRequest,
    _user: UserModel = Depends(get_superuser),
):
    """Store Telnyx dialer settings; the API key is encrypted by the DB model."""
    current = await _telnyx_dialer_credentials()
    api_key = (body.api_key or current.get("api_key") or "").strip()
    connection_id = body.connection_id.strip()
    if not api_key:
        raise HTTPException(status_code=422, detail="Enter a Telnyx API key.")
    if not connection_id:
        raise HTTPException(
            status_code=422,
            detail="Enter the Telnyx SIP connection ID.",
        )

    # On-demand WebRTC credentials are API resources attached to a credential
    # connection. They are not displayed as the connection's ID in Mission
    # Control. Reuse an existing credential for this connection where possible;
    # otherwise create one so setup only requires the API key and connection ID.
    credential_id = (current.get("telephony_credential_id") or "").strip()
    async with httpx.AsyncClient(timeout=15.0) as client:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        existing_is_valid = False
        if credential_id and current.get("connection_id") == connection_id:
            existing = await _telnyx_request(
                client, "GET", f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}",
                headers=headers,
            )
            if existing.is_success:
                existing_data = existing.json().get("data") or {}
                existing_is_valid = (
                    existing_data.get("resource_id") == f"connection:{connection_id}"
                    and not existing_data.get("expired", False)
                )
            elif existing.status_code not in (404, 422):
                logger.error(f"Telnyx credential validation failed: HTTP {existing.status_code}")
                raise HTTPException(status_code=502, detail="Telnyx could not validate the saved WebRTC credential.")

        if not existing_is_valid:
            # Credentials created in the portal/API may already be attached to
            # this connection. Find and reuse one before creating another.
            found_existing_id = False
            page = 1
            while True:
                listed = await _telnyx_request(
                    client, "GET", "https://api.telnyx.com/v2/telephony_credentials",
                    params={"page[number]": page, "page[size]": 250},
                    headers=headers,
                )
                if listed.status_code in (401, 403):
                    raise HTTPException(status_code=502, detail="Telnyx rejected the API key or its credential permissions.")
                if not listed.is_success:
                    logger.error(f"Telnyx credential lookup failed: HTTP {listed.status_code}")
                    raise HTTPException(status_code=502, detail="Could not check Telnyx WebRTC credentials.")
                payload = listed.json()
                match = next(
                    (item for item in payload.get("data", [])
                     if item.get("resource_id") == f"connection:{connection_id}"
                     and item.get("id") and not item.get("expired", False)),
                    None,
                )
                if match:
                    credential_id = match["id"]
                    found_existing_id = True
                    break
                meta = payload.get("meta") or {}
                if page >= (meta.get("total_pages") or page) or not payload.get("data"):
                    break
                page += 1

            if not found_existing_id:
                created = await _telnyx_request(
                    client, "POST", "https://api.telnyx.com/v2/telephony_credentials",
                    headers=headers,
                    json={"connection_id": connection_id, "name": "Sysevo Dialer"},
                )
                if created.status_code in (401, 403):
                    raise HTTPException(status_code=502, detail="Telnyx rejected the API key or its permission to create WebRTC credentials.")
                if not created.is_success:
                    logger.error(f"Telnyx WebRTC credential creation failed: HTTP {created.status_code}")
                    raise HTTPException(status_code=502, detail="Telnyx could not create a WebRTC credential for this SIP connection. Check the connection ID and API key permissions.")
                credential_id = (created.json().get("data") or {}).get("id", "")
                if not credential_id:
                    raise HTTPException(status_code=502, detail="Telnyx created no usable WebRTC credential.")

    await db_client.save_platform_telnyx_dialer_credentials(
        api_key=api_key,
        connection_id=connection_id,
        telephony_credential_id=credential_id,
    )
    return _telnyx_credentials_response(
        await db_client.get_platform_telnyx_dialer_credentials()
    )


@router.post("/telnyx-dialer/check")
async def check_telnyx_dialer_connection(_user: UserModel = Depends(get_superuser)):
    """Verify the saved Telnyx connection can issue WebRTC tokens and route PSTN calls.

    This is a configuration check only. It does not place a live call or promise
    that a downstream carrier will deliver a specific call.
    """
    credentials = await _telnyx_dialer_credentials()
    api_key = (credentials.get("api_key") or "").strip()
    connection_id = (credentials.get("connection_id") or "").strip()
    credential_id = (credentials.get("telephony_credential_id") or "").strip()
    if not api_key or not connection_id or not credential_id:
        raise HTTPException(status_code=409, detail="Save the Telnyx API key and SIP connection first.")

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        connection_response = await _telnyx_request(
            client,
            "GET",
            f"https://api.telnyx.com/v2/credential_connections/{connection_id}",
            headers=headers,
        )
        if connection_response.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Telnyx rejected the API key or SIP connection access.")
        if not connection_response.is_success:
            raise HTTPException(status_code=502, detail="Telnyx could not find the saved SIP connection. Check its ID.")
        connection = connection_response.json().get("data") or {}
        active = connection.get("active") is True
        outbound = connection.get("outbound") or {}
        profile_id = str(outbound.get("outbound_voice_profile_id") or "").strip()
        profile_enabled = False
        us_destination_enabled = False
        if profile_id:
            profile_response = await _telnyx_request(
                client,
                "GET",
                f"https://api.telnyx.com/v2/outbound_voice_profiles/{profile_id}",
                headers=headers,
            )
            if profile_response.status_code in (401, 403):
                raise HTTPException(status_code=502, detail="Telnyx rejected access to the outbound voice profile.")
            if not profile_response.is_success:
                raise HTTPException(status_code=502, detail="Telnyx could not load the outbound voice profile assigned to this connection.")
            profile = profile_response.json().get("data") or {}
            profile_enabled = profile.get("enabled") is True
            destinations = profile.get("whitelisted_destinations") or []
            us_destination_enabled = any(
                str(destination).strip().upper() in {"US", "ALL", "WORLD"}
                for destination in destinations
            ) if isinstance(destinations, list) else False

        credential_response = await _telnyx_request(
            client,
            "GET",
            f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}",
            headers=headers,
        )
        if credential_response.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Telnyx rejected access to the dialer's WebRTC credential.")
        if not credential_response.is_success:
            raise HTTPException(status_code=502, detail="The saved WebRTC credential is unavailable in Telnyx.")
        credential = credential_response.json().get("data") or {}
        credential_matches = (
            credential.get("resource_id") == f"connection:{connection_id}"
            and not credential.get("expired", False)
        )
        token_issued = False
        if credential_matches:
            token_response = await _telnyx_request(
                client,
                "POST",
                f"https://api.telnyx.com/v2/telephony_credentials/{credential_id}/token",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "text/plain"},
            )
            token_issued = token_response.is_success and bool(token_response.text.strip().strip('"'))

        numbers_response = await _telnyx_request(
            client,
            "GET",
            "https://api.telnyx.com/v2/phone_numbers",
            params={"page[number]": 1, "page[size]": 250, "filter[connection_id]": connection_id},
            headers=headers,
        )
        if numbers_response.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Telnyx rejected access to the phone number inventory.")
        if not numbers_response.is_success:
            raise HTTPException(status_code=502, detail="Telnyx could not verify numbers on the SIP connection.")
        numbers = [
            item for item in (numbers_response.json().get("data") or [])
            if item.get("connection_id") == connection_id
        ]

    checks = {
        "connection_active": active,
        "outbound_voice_profile_assigned": bool(profile_id),
        "outbound_voice_profile_enabled": profile_enabled,
        "us_destination_enabled": us_destination_enabled,
        "webrtc_credential_matches_connection": credential_matches,
        "webrtc_token_issued": token_issued,
        "phone_numbers_attached": len(numbers),
    }
    warnings = []
    if not active:
        warnings.append("The Telnyx SIP connection is inactive.")
    if not profile_id:
        warnings.append("Assign an outbound voice profile to this SIP connection to route PSTN calls.")
    elif not profile_enabled:
        warnings.append("Enable the outbound voice profile assigned to this connection.")
    if profile_id and not us_destination_enabled:
        warnings.append("Add United States (US) to the assigned outbound voice profile's allowed destinations.")
    if not credential_matches:
        warnings.append("The WebRTC credential is not attached to the saved SIP connection.")
    elif not token_issued:
        warnings.append("Telnyx could not issue a WebRTC login token for this connection.")
    if not numbers:
        warnings.append("No Telnyx numbers are attached to this SIP connection.")
    ready_for_outbound = (
        active
        and bool(profile_id)
        and profile_enabled
        and us_destination_enabled
        and credential_matches
        and token_issued
        and bool(numbers)
    )
    return {
        "ready_for_outbound": ready_for_outbound,
        "checks": checks,
        "warnings": warnings,
        "live_call_test_required": True,
    }


@router.post("/telnyx-dialer/sync-numbers")
async def sync_telnyx_dialer_numbers(_user: UserModel = Depends(get_superuser)):
    """Sync Telnyx numbers attached to the configured SIP connection."""
    credentials = await _telnyx_dialer_credentials()
    api_key = (credentials.get("api_key") or "").strip()
    connection_id = (credentials.get("connection_id") or "").strip()
    if not api_key or not connection_id:
        raise HTTPException(status_code=409, detail="Save the Telnyx API key and SIP connection ID first.")
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Dialer inventory sync is not configured on the server.")

    numbers: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            response = await client.get(
                "https://api.telnyx.com/v2/phone_numbers",
                params={
                    "page[number]": page,
                    "page[size]": 250,
                    "filter[connection_id]": connection_id,
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code in (401, 403):
                raise HTTPException(
                    status_code=502,
                    detail="Telnyx rejected access to the number inventory. Check the API key, KYC status, and connection permissions.",
                )
            if not response.is_success:
                logger.error(f"[admin_telephony] Telnyx number sync failed: HTTP {response.status_code}")
                raise HTTPException(status_code=502, detail="Telnyx number inventory request failed.")
            payload = response.json()
            for number in payload.get("data") or []:
                if number.get("connection_id") != connection_id:
                    continue
                numbers.append({
                    "phone_number": number["phone_number"],
                    "friendly_name": number.get("connection_name") or None,
                    "provider": "telnyx",
                    "twilio_sid": None,
                })
            if page >= (payload.get("meta") or {}).get("total_pages", page):
                break
            page += 1

        for start in range(0, len(numbers), 500):
            batch = numbers[start : start + 500]
            response = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/rest/v1/dialer_phone_numbers?on_conflict=phone_number",
                json=batch,
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
            )
            if not response.is_success:
                logger.error(f"[admin_telephony] Telnyx inventory upsert failed: HTTP {response.status_code}")
                raise HTTPException(status_code=502, detail="Could not save Telnyx numbers to the dialer inventory.")
    return {"synced": len(numbers), "connection_id": connection_id}


def _mask_sid(sid: Optional[str]) -> Optional[str]:
    return (sid[:6] + "****") if sid else None


async def _resolve_platform_sid() -> tuple[Optional[str], Optional[str]]:
    """
    Return (account_sid, source) for the active platform Twilio account.
    DB-stored credentials win over env vars; returns (None, None) if neither.
    """
    db_sid = await db_client.get_platform_twilio_sid()
    if db_sid:
        return db_sid, "database"
    env_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    if env_sid and os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN"):
        return env_sid, "environment"
    return None, None


@router.get("/status", response_model=ManagedStatusResponse)
async def managed_status(_user: UserModel = Depends(get_superuser)):
    """Check whether the platform Twilio account is configured (DB or env)."""
    sid, source = await _resolve_platform_sid()
    return ManagedStatusResponse(
        configured=sid is not None,
        account_sid_preview=_mask_sid(sid),
        source=source,
    )


def _validate_twilio_credentials(account_sid: str, auth_token: str) -> str:
    """Fetch the account as a cheap authenticated validation call. Raises TwilioRestException on failure."""
    client = Client(account_sid, auth_token)
    account = client.api.accounts(account_sid).fetch()
    return account.friendly_name or account_sid


@router.get("/accounts", response_model=PlatformTwilioAccountsResponse)
async def list_twilio_accounts(_user: UserModel = Depends(get_superuser)):
    """List every stored platform Twilio account."""
    rows = await db_client.list_platform_twilio_accounts()
    env_fallback = bool(
        os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
        and os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    )
    return PlatformTwilioAccountsResponse(
        accounts=[
            PlatformTwilioAccountItem(
                id=r["id"],
                label=r["label"],
                account_sid_preview=_mask_sid(r["account_sid"]) or "",
                is_active=r["is_active"],
                last_validated_at=(
                    r["last_validated_at"].isoformat() if r["last_validated_at"] else None
                ),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
                dialer_configured=r["dialer_configured"],
                dialer_api_key_sid=r["dialer_api_key_sid"],
                dialer_twiml_app_sid=r["dialer_twiml_app_sid"],
                dialer_default_caller_id=r["dialer_default_caller_id"],
            )
            for r in rows
        ],
        env_fallback_configured=env_fallback,
    )


@router.post("/accounts", response_model=AddTwilioAccountResponse)
async def add_twilio_account(
    body: AddTwilioAccountRequest,
    _user: UserModel = Depends(get_superuser),
):
    """
    Validate and store a new platform-level Twilio account (auth token
    encrypted at rest). Existing accounts are left untouched unless
    ``make_active`` is set. Credentials are verified against Twilio before
    saving so a bad SID/token is rejected up front.
    """
    account_sid = body.account_sid.strip()
    auth_token = body.auth_token.strip()
    label = body.label.strip() if body.label else None
    if not account_sid or not auth_token:
        raise HTTPException(status_code=422, detail="account_sid and auth_token are required.")
    if not account_sid.startswith("AC"):
        raise HTTPException(status_code=422, detail="account_sid should start with 'AC'.")

    try:
        friendly_name = await asyncio.to_thread(
            _validate_twilio_credentials, account_sid, auth_token
        )
    except TwilioRestException as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Twilio rejected these credentials: {exc.msg}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[admin_telephony] Credential validation failed: {exc}")
        raise HTTPException(status_code=400, detail="Could not validate credentials with Twilio.")

    account_id = await db_client.add_platform_twilio_account(
        account_sid,
        auth_token,
        label=label,
        make_active=body.make_active,
        dialer_api_key_sid=(body.dialer_api_key_sid.strip() if body.dialer_api_key_sid else None),
        dialer_api_key_secret=(
            body.dialer_api_key_secret.strip() if body.dialer_api_key_secret else None
        ),
        dialer_twiml_app_sid=(
            body.dialer_twiml_app_sid.strip() if body.dialer_twiml_app_sid else None
        ),
        dialer_default_caller_id=(
            body.dialer_default_caller_id.strip() if body.dialer_default_caller_id else None
        ),
    )
    logger.info(
        f"[admin_telephony] Platform Twilio account added "
        f"(id={account_id}, sid={_mask_sid(account_sid)}, active={body.make_active})"
    )
    return AddTwilioAccountResponse(id=account_id, friendly_name=friendly_name)


@router.put("/accounts/{account_id}", response_model=PlatformTwilioAccountsResponse)
async def update_twilio_account(
    account_id: int,
    body: UpdateTwilioAccountRequest,
    _user: UserModel = Depends(get_superuser),
):
    """
    Partially update a stored account — label, credentials, and/or dialer
    fields. Only fields present in the request body are touched.
    """
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if "account_sid" in updates or "auth_token" in updates:
        if not (updates.get("account_sid") and updates.get("auth_token")):
            raise HTTPException(
                status_code=422,
                detail="account_sid and auth_token must be updated together.",
            )
        account_sid = updates["account_sid"].strip()
        auth_token = updates["auth_token"].strip()
        if not account_sid.startswith("AC"):
            raise HTTPException(status_code=422, detail="account_sid should start with 'AC'.")
        try:
            await asyncio.to_thread(_validate_twilio_credentials, account_sid, auth_token)
        except TwilioRestException as exc:
            raise HTTPException(
                status_code=400, detail=f"Twilio rejected these credentials: {exc.msg}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[admin_telephony] Credential validation failed: {exc}")
            raise HTTPException(status_code=400, detail="Could not validate credentials with Twilio.")
        updates["account_sid"] = account_sid
        updates["auth_token"] = auth_token

    for key in ("label", "dialer_api_key_sid", "dialer_twiml_app_sid", "dialer_default_caller_id"):
        if key in updates and updates[key] is not None:
            updates[key] = updates[key].strip()
    if updates.get("dialer_api_key_secret"):
        updates["dialer_api_key_secret"] = updates["dialer_api_key_secret"].strip()

    error = await db_client.update_platform_twilio_account(account_id, updates)
    if error:
        raise HTTPException(status_code=404, detail=error)
    logger.info(f"[admin_telephony] Platform Twilio account {account_id} updated: {sorted(updates.keys())}")
    return await list_twilio_accounts(_user)


@router.post("/accounts/{account_id}/activate", response_model=PlatformTwilioAccountsResponse)
async def activate_twilio_account(
    account_id: int, _user: UserModel = Depends(get_superuser)
):
    """Make *account_id* the sole active platform Twilio account."""
    ok = await db_client.set_active_platform_twilio_account(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="account not found")
    logger.info(f"[admin_telephony] Platform Twilio account {account_id} activated")
    return await list_twilio_accounts(_user)


@router.delete("/accounts/{account_id}", response_model=PlatformTwilioAccountsResponse)
async def delete_twilio_account(
    account_id: int, _user: UserModel = Depends(get_superuser)
):
    """Delete a stored platform Twilio account. Refuses to delete the active one."""
    error = await db_client.delete_platform_twilio_account(account_id)
    if error:
        status = 404 if error == "account not found" else 400
        raise HTTPException(status_code=status, detail=error)
    logger.info(f"[admin_telephony] Platform Twilio account {account_id} deleted")
    return await list_twilio_accounts(_user)


@router.get("/numbers", response_model=ManagedNumbersResponse)
async def list_managed_numbers(_user: UserModel = Depends(get_superuser)):
    """List all Sysevo-managed phone numbers across every org."""
    # account_sid -> label, for resolving which platform Twilio account a
    # managed number was bought under. Built once, not per-row.
    accounts_by_sid = {
        a["account_sid"]: (a["label"] or _mask_sid(a["account_sid"]))
        for a in await db_client.list_platform_twilio_accounts()
    }

    async with db_client.async_session() as session:
        stmt = (
            select(TelephonyPhoneNumberModel, OrganizationModel, WorkflowModel, TelephonyConfigurationModel)
            .join(
                TelephonyConfigurationModel,
                TelephonyPhoneNumberModel.telephony_configuration_id
                == TelephonyConfigurationModel.id,
            )
            .join(
                OrganizationModel,
                TelephonyConfigurationModel.organization_id == OrganizationModel.id,
            )
            .outerjoin(
                WorkflowModel,
                TelephonyPhoneNumberModel.inbound_workflow_id == WorkflowModel.id,
            )
            .where(
                # extra_metadata is a generic JSON column (not JSONB); the ORM
                # `[...].astext` accessor mis-renders against it and matched 0
                # rows. json_extract_path_text emits the same `->>` operator that
                # correctly matches the stored is_managed value.
                func.json_extract_path_text(
                    TelephonyPhoneNumberModel.extra_metadata, "is_managed"
                )
                == "true"
            )
            .order_by(TelephonyPhoneNumberModel.id.desc())
        )
        result = await session.execute(stmt)
        rows = result.all()

        # Calls run through each number = runs of its inbound workflow.
        wf_ids = [num.inbound_workflow_id for num, _, _, _ in rows if num.inbound_workflow_id]
        run_counts: dict[int, int] = {}
        if wf_ids:
            run_stmt = (
                select(WorkflowRunModel.workflow_id, func.count(WorkflowRunModel.id))
                .where(WorkflowRunModel.workflow_id.in_(wf_ids))
                .group_by(WorkflowRunModel.workflow_id)
            )
            run_counts = {wf: cnt for wf, cnt in (await session.execute(run_stmt)).all()}

    items = []
    total_cost = 0
    for num, org, workflow, cfg in rows:
        meta = num.extra_metadata or {}
        raw_sid = meta.get("managed_twilio_sid", "")
        sid_preview = (raw_sid[:6] + "****") if raw_sid else None
        cost = _NUMBER_MONTHLY_COST_CENTS.get(
            (num.country_code or "").upper(), _DEFAULT_MONTHLY_COST_CENTS
        )
        total_cost += cost
        platform_account_sid = (cfg.credentials or {}).get("account_sid") if cfg else None
        items.append(
            ManagedNumberItem(
                phone_number_id=num.id,
                address=num.address_normalized or num.address,
                country_code=num.country_code,
                label=num.label,
                organization_id=org.id,
                organization_name=org.provider_id,
                inbound_workflow_id=num.inbound_workflow_id,
                inbound_workflow_name=workflow.name if workflow else None,
                twilio_sid_preview=sid_preview,
                is_active=num.is_active,
                created_at=(
                    num.created_at.isoformat()
                    if getattr(num, "created_at", None)
                    else None
                ),
                monthly_cost_cents=cost,
                call_count=run_counts.get(num.inbound_workflow_id, 0),
                platform_account_label=accounts_by_sid.get(platform_account_sid),
                platform_account_sid_preview=_mask_sid(platform_account_sid),
            )
        )

    return ManagedNumbersResponse(
        numbers=items, total=len(items), total_monthly_cost_cents=total_cost
    )
