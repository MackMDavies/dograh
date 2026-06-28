"""
Quick Connect telephony endpoints.

These let Sysevo provision and manage numbers on behalf of users via the
platform-level Twilio account (SYSEVO_TWILIO_ACCOUNT_SID / AUTH_TOKEN).
Users never supply credentials — Sysevo handles Twilio internally.
"""
import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.telephony.managed_provisioner import get_managed_provisioner
from api.utils.common import get_backend_endpoints

router = APIRouter(prefix="/telephony", tags=["telephony"])

_MANAGED_CONFIG_NAME = "Sysevo Managed"
# Stored as "twilio" so the existing inbound dispatcher can match the webhook
# via credentials["account_sid"] == platform account SID.
_MANAGED_PROVIDER = "twilio"


# ── Schemas ───────────────────────────────────────────────────────────────────

class QuickConnectRequest(BaseModel):
    mode: Literal["forward", "new"]
    existing_number: Optional[str] = None  # E.164 — required for mode=forward
    country: str                            # ISO 3166-1 alpha-2
    area_code: Optional[str] = None
    workflow_id: Optional[int] = None


class QuickConnectResponse(BaseModel):
    managed_number: str
    telephony_config_id: int
    phone_number_id: int


class CarrierLookupResponse(BaseModel):
    carrier: Optional[str]
    country: str
    line_type: str


class AvailableNumbersResponse(BaseModel):
    numbers: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/carrier-lookup", response_model=CarrierLookupResponse)
async def carrier_lookup(
    number: str = Query(..., description="E.164 phone number to look up"),
    user: UserModel = Depends(get_user),
):
    """Detect carrier and line type for *number* via Twilio Lookup v2."""
    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(
            status_code=503,
            detail="Sysevo managed telephony is not configured on this instance.",
        )
    result = await asyncio.to_thread(provisioner.lookup_carrier, number)
    return CarrierLookupResponse(**result)


@router.get("/available-numbers", response_model=AvailableNumbersResponse)
async def available_numbers(
    country: str = Query(..., description="ISO 3166-1 alpha-2 country code"),
    area_code: Optional[str] = Query(None),
    user: UserModel = Depends(get_user),
):
    """List purchasable numbers from the platform Twilio account."""
    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(status_code=503, detail="Managed telephony not configured.")
    numbers = await asyncio.to_thread(provisioner.search_available_numbers, country, area_code)
    return AvailableNumbersResponse(numbers=numbers)


@router.post("/quick-connect", response_model=QuickConnectResponse)
async def quick_connect(
    body: QuickConnectRequest,
    user: UserModel = Depends(get_user),
):
    """
    Provision a Sysevo-managed number for this organisation.

    - mode=forward: provisions a destination number; caller forwards their
      existing carrier number to it.
    - mode=new: provisions the chosen/searched number as the org's primary line.
    """
    if body.mode == "forward" and not body.existing_number:
        raise HTTPException(status_code=422, detail="existing_number is required for mode=forward")

    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(status_code=503, detail="Managed telephony not configured.")

    org_id = user.selected_organization_id
    # Use the resolved credentials (DB-stored or env) so the managed config's
    # stored account_sid matches inbound Twilio webhooks regardless of source.
    platform_sid = provisioner.account_sid
    platform_token = provisioner.auth_token
    backend_url, _ = await get_backend_endpoints()
    # /inbound/run is the workflow-agnostic dispatcher; /twiml needs pre-known params
    voice_url = f"{backend_url}/api/v1/telephony/inbound/run"

    # Determine the number to purchase
    target_country = (body.country or "US").upper()
    if body.mode == "new" and body.existing_number:
        # Path B: user already picked a specific number from /available-numbers
        target_e164 = body.existing_number
    else:
        # Path A or Path B without a specific number: find one in the right country
        numbers = await asyncio.to_thread(
            provisioner.search_available_numbers, body.country, body.area_code, 1
        )
        if not numbers:
            raise HTTPException(
                status_code=409,
                detail=f"No numbers available in {body.country}. Try a different area code or country.",
            )
        target_e164 = numbers[0]

    # Some countries (e.g. GB) require a registered Twilio Address to buy local
    # numbers. Attach one automatically if the platform account has it.
    address_sid = await asyncio.to_thread(provisioner.get_address_sid, target_country)

    # Provision on Twilio
    try:
        provisioned = await asyncio.to_thread(
            provisioner.provision_number, target_e164, voice_url, address_sid
        )
    except TwilioRestException as exc:
        err_lower = (exc.msg or "").lower()
        # Twilio blocks local-number purchase in regulated countries (e.g. GB)
        # until a registered Address and/or Regulatory Bundle is supplied.
        needs_regulatory = any(
            token in err_lower
            for token in (
                "requires an address",
                "addresssid",
                "bundle required",
                "bundle is required",
                "regulatory bundle",
                "not provided for country",
            )
        )
        if needs_regulatory and target_country != "US":
            # The chosen country needs regulatory paperwork (Address + Bundle) we
            # don't have, so the purchase is blocked. Fall back to a US number,
            # which has no such requirement, so the flow always completes. For a
            # forwarded number the caller still dials the user's own number; for a
            # new number the user gets a working Sysevo line. A true local number
            # requires completing Twilio regulatory compliance for that country.
            us_numbers = await asyncio.to_thread(
                provisioner.search_available_numbers, "US", None, 1
            )
            if not us_numbers:
                raise HTTPException(
                    status_code=502,
                    detail=f"Twilio provisioning failed: {exc.msg}",
                )
            try:
                provisioned = await asyncio.to_thread(
                    provisioner.provision_number, us_numbers[0], voice_url
                )
                target_country = "US"
            except TwilioRestException as exc2:
                raise HTTPException(
                    status_code=502,
                    detail=f"Twilio provisioning failed: {exc2.msg}",
                )
        else:
            raise HTTPException(
                status_code=502, detail=f"Twilio provisioning failed: {exc.msg}"
            )

    try:
        # Find or create a "Sysevo Managed" telephony config for this org.
        # Matched by name (not provider) because provider="twilio" could also
        # match the org's own Twilio configs.
        configs = await db_client.list_telephony_configurations(org_id)
        managed_config = next((c for c in configs if c.name == _MANAGED_CONFIG_NAME), None)

        if managed_config is None:
            # Credentials stored so the Twilio inbound dispatcher can match the
            # platform account_sid from the webhook and verify the signature.
            managed_config = await db_client.create_telephony_configuration(
                organization_id=org_id,
                name=_MANAGED_CONFIG_NAME,
                provider=_MANAGED_PROVIDER,
                credentials={"account_sid": platform_sid, "auth_token": platform_token},
                is_default_outbound=False,
            )

        # Validate workflow_id belongs to this org before attaching
        workflow_id = None
        if body.workflow_id:
            wf = await db_client.get_workflow(body.workflow_id, organization_id=org_id)
            if wf is None:
                raise HTTPException(status_code=404, detail="Workflow not found.")
            workflow_id = wf.id

        # Store the phone number
        phone_row = await db_client.create_phone_number(
            organization_id=org_id,
            telephony_configuration_id=managed_config.id,
            address=provisioned.e164,
            country_code=target_country,
            label=f"Sysevo ({target_country})",
            inbound_workflow_id=workflow_id,
            extra_metadata={
                "is_managed": True,
                "managed_twilio_sid": provisioned.twilio_sid,
                "forwarding_from": body.existing_number,
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions directly (e.g. workflow not found)
        # but first release the provisioned number to avoid a Twilio leak
        await asyncio.to_thread(provisioner.release_number, provisioned.twilio_sid)
        raise
    except Exception:
        await asyncio.to_thread(provisioner.release_number, provisioned.twilio_sid)
        raise HTTPException(status_code=500, detail="Failed to save provisioned number. The Twilio number has been released.")

    return QuickConnectResponse(
        managed_number=provisioned.e164,
        telephony_config_id=managed_config.id,
        phone_number_id=phone_row.id,
    )


@router.delete("/managed-numbers/{phone_number_id}", status_code=204)
async def delete_managed_number(
    phone_number_id: int,
    user: UserModel = Depends(get_user),
):
    """Release a Sysevo-managed number back to Twilio and remove DB records."""
    org_id = user.selected_organization_id
    row = await db_client.get_phone_number(phone_number_id, organization_id=org_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found.")

    meta = row.extra_metadata or {}
    if not meta.get("is_managed"):
        raise HTTPException(status_code=400, detail="This number is not a Sysevo-managed number.")

    twilio_sid = meta.get("managed_twilio_sid")

    # Delete the DB row first so the record is gone even if Twilio release fails.
    await db_client.delete_phone_number(phone_number_id, organization_id=org_id)

    if twilio_sid:
        provisioner = await get_managed_provisioner()
        if provisioner:
            released = await asyncio.to_thread(provisioner.release_number, twilio_sid)
            if not released:
                logger.warning(f"[quick_connect] Failed to release Twilio SID {twilio_sid} for phone_number_id={phone_number_id}")
