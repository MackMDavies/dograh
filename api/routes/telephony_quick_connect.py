"""
Quick Connect telephony endpoints.

These let Sysevo provision and manage numbers on behalf of users via the
platform-level Twilio account (SYSEVO_TWILIO_ACCOUNT_SID / AUTH_TOKEN).
Users never supply credentials — Sysevo handles Twilio internally.
"""
import asyncio
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException

from api.db import db_client
from api.db.models import UserModel
from api.enums import OrganizationConfigurationKey
from api.services.auth.depends import get_user
from api.services.org_concurrency import clamp_to_system_max
from api.services.telephony.managed_provisioner import (
    ManagedProvisioner,
    RegulationNotAutomatable,
    get_managed_provisioner,
)
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
    # Superuser-only: buy under a specific platform Twilio account instead of
    # whichever is active. Silently ignored for non-superusers — regular
    # users never choose or see Twilio credentials, by design (see module
    # docstring).
    platform_account_id: Optional[int] = None
    # The CLIENT's own approved Regulatory Bundle (and its Address) for a regulated
    # country, built by POST /telephony/regulatory/bundles from their KYC. Required to
    # buy a local number in e.g. GB; never looked up platform-wide (see quick_connect).
    address_sid: Optional[str] = None
    bundle_sid: Optional[str] = None


class RegulationResponse(BaseModel):
    country: str
    requires_bundle: bool
    regulation_sid: Optional[str] = None


class BundleRequest(BaseModel):
    country: str
    # {business:{name, registration_identifier, registration_number, website},
    #  representative:{first_name, last_name, email, phone},
    #  address:{street, street2, city, region, postal_code, country}}
    kyc: dict
    email: str
    status_callback: Optional[str] = None


class BundleResponse(BaseModel):
    bundle_sid: str
    address_sid: str
    status: str
    failures: list = []


class BundleStatusResponse(BaseModel):
    sid: str
    status: str
    iso_country: Optional[str] = None
    valid_until: Optional[str] = None
    failures: list = []


def _regulatory_error(code: str, message: str, status: int = 409, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


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

def _resolve_platform_account_id(
    user: UserModel, requested: Optional[int]
) -> Optional[int]:
    """Superusers may pick a specific platform account; everyone else gets None (= active one)."""
    return requested if (requested and user.is_superuser) else None


async def _provisioner_for_number(row) -> Optional[ManagedProvisioner]:
    """
    Resolve the ManagedProvisioner that actually owns *row* on Twilio — its
    own telephony_configuration's stored credentials, not "whichever platform
    account is active right now". A number bought under a non-default
    account can never be released via a different account's client; Twilio
    scopes phone-number resources to the account that owns them.
    """
    cfg = await db_client.get_telephony_configuration(row.telephony_configuration_id)
    creds = (cfg.credentials or {}) if cfg else {}
    account_sid, auth_token = creds.get("account_sid"), creds.get("auth_token")
    if account_sid and auth_token:
        return ManagedProvisioner(account_sid=account_sid, auth_token=auth_token)
    # Legacy row whose config predates credential storage, or credentials
    # failed to decrypt — fall back to the active account rather than fail
    # outright; the release may 404 on Twilio if it's genuinely the wrong
    # account, but the DB row is deleted regardless (see call sites).
    return await get_managed_provisioner()


@router.get("/carrier-lookup", response_model=CarrierLookupResponse)
async def carrier_lookup(
    number: str = Query(..., description="E.164 phone number to look up"),
    platform_account_id: Optional[int] = Query(None),
    user: UserModel = Depends(get_user),
):
    """Detect carrier and line type for *number* via Twilio Lookup v2."""
    provisioner = await get_managed_provisioner(
        _resolve_platform_account_id(user, platform_account_id)
    )
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
    platform_account_id: Optional[int] = Query(None),
    user: UserModel = Depends(get_user),
):
    """List purchasable numbers from the platform Twilio account."""
    provisioner = await get_managed_provisioner(
        _resolve_platform_account_id(user, platform_account_id)
    )
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

    chosen_account_id = _resolve_platform_account_id(user, body.platform_account_id)
    provisioner = await get_managed_provisioner(chosen_account_id)
    if provisioner is None:
        detail = (
            "That Twilio account no longer exists."
            if chosen_account_id
            else "Managed telephony not configured."
        )
        raise HTTPException(status_code=503, detail=detail)

    org_id = user.selected_organization_id
    # Use the resolved credentials (DB-stored or env) so the managed config's
    # stored account_sid matches inbound Twilio webhooks regardless of source.
    platform_sid = provisioner.account_sid
    platform_token = provisioner.auth_token
    backend_url, _ = await get_backend_endpoints()
    # /inbound/run is the workflow-agnostic dispatcher; /twiml needs pre-known params
    voice_url = f"{backend_url}/api/v1/telephony/inbound/run"

    # Determine the number to purchase. No default country: guessing US bought a US
    # number for a UK business.
    target_country = body.country.upper()

    # REGULATED COUNTRIES NEED THE CLIENT'S OWN APPROVED BUNDLE, checked BEFORE buying.
    # This used to look up "any approved bundle for the country" in the platform account
    # -- which, once bundles are per client, could attach client A's bundle to client
    # B's number -- and, failing that, silently bought a US line. Neither happens now.
    if body.bundle_sid:
        try:
            bundle = await asyncio.to_thread(provisioner.get_bundle, body.bundle_sid)
        except TwilioRestException:
            raise _regulatory_error(
                "regulatory_bundle_not_found",
                "That regulatory bundle does not exist in the account that buys numbers.",
            )
        if bundle.get("status") != "twilio-approved":
            raise _regulatory_error(
                "regulatory_bundle_not_approved",
                f"The regulatory bundle is {bundle.get('status')}; a number can only be bought once Twilio approves it.",
                status_detail=bundle.get("status"),
            )
        if (bundle.get("iso_country") or "").upper() != target_country:
            raise _regulatory_error(
                "regulatory_bundle_wrong_country",
                f"The regulatory bundle is for {bundle.get('iso_country')}, not {target_country}.",
            )
        address_sid, bundle_sid = body.address_sid, body.bundle_sid
    else:
        address_sid = bundle_sid = None
        try:
            regulation = await asyncio.to_thread(provisioner.regulation_for, target_country)
        except TwilioRestException as exc:
            # Could not ask; let the purchase decide rather than block on a lookup.
            logger.warning(f"[quick_connect] regulation lookup failed for {target_country}: {exc}")
            regulation = {"requires_bundle": False}
        if regulation.get("requires_bundle"):
            raise _regulatory_error(
                "regulatory_bundle_required",
                f"Local numbers in {target_country} need an approved regulatory bundle for this business.",
                country=target_country,
            )

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

    # Provision on Twilio
    try:
        provisioned = await asyncio.to_thread(
            provisioner.provision_number, target_e164, voice_url, address_sid, bundle_sid
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
        if needs_regulatory:
            # No silent US fallback: a UK business given a +1 line has callers dialling
            # internationally, and nobody notices until they complain.
            raise _regulatory_error(
                "regulatory_bundle_required",
                f"Twilio needs an approved regulatory bundle for local numbers in {target_country}: {exc.msg}",
                country=target_country,
            )
        raise HTTPException(status_code=502, detail=f"Twilio provisioning failed: {exc.msg}")

    try:
        # Find or create a "Sysevo Managed" telephony config for this org,
        # scoped to the platform account actually used for THIS purchase —
        # not just by name. An admin choosing a different platform_account_id
        # on a later purchase must get its own config, or the number would be
        # silently attached to the wrong Twilio account's credentials.
        configs = await db_client.list_telephony_configurations(org_id)
        managed_config = next(
            (
                c for c in configs
                if c.name == _MANAGED_CONFIG_NAME
                and (c.credentials or {}).get("account_sid") == platform_sid
            ),
            None,
        )

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


@router.get("/regulatory/requirements", response_model=RegulationResponse)
async def regulatory_requirements(
    country: str = Query(..., description="ISO 3166-1 alpha-2 country code"),
    user: UserModel = Depends(get_user),
):
    """Whether a local business number in *country* needs a regulatory bundle."""
    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(status_code=503, detail="Managed telephony not configured.")
    try:
        reg = await asyncio.to_thread(provisioner.regulation_for, country)
    except TwilioRestException as exc:
        raise HTTPException(status_code=502, detail=f"Twilio regulation lookup failed: {exc.msg}")
    return RegulationResponse(country=country.upper(), requires_bundle=reg["requires_bundle"], regulation_sid=reg["sid"])


@router.post("/regulatory/bundles", response_model=BundleResponse)
async def create_regulatory_bundle(
    body: BundleRequest,
    user: UserModel = Depends(get_user),
):
    """
    Build and submit ONE client's regulatory bundle from their KYC, in the account that
    buys numbers (bundles cannot be shared across Twilio accounts). Review takes Twilio
    about one business day; poll GET /regulatory/bundles/{sid}.
    """
    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(status_code=503, detail="Managed telephony not configured.")
    try:
        out = await asyncio.to_thread(
            provisioner.create_bundle, body.country, body.kyc, body.email, body.status_callback
        )
    except RegulationNotAutomatable as exc:
        raise _regulatory_error("regulation_not_automatable", str(exc), status=422)
    except ValueError as exc:
        raise _regulatory_error("kyc_incomplete", str(exc), status=422)
    except TwilioRestException as exc:
        raise HTTPException(status_code=502, detail=f"Twilio bundle creation failed: {exc.msg}")
    return BundleResponse(**out)


@router.get("/regulatory/bundles/{bundle_sid}", response_model=BundleStatusResponse)
async def regulatory_bundle_status(
    bundle_sid: str,
    user: UserModel = Depends(get_user),
):
    provisioner = await get_managed_provisioner()
    if provisioner is None:
        raise HTTPException(status_code=503, detail="Managed telephony not configured.")
    try:
        out = await asyncio.to_thread(provisioner.get_bundle, bundle_sid)
    except TwilioRestException as exc:
        raise HTTPException(status_code=404 if exc.status == 404 else 502, detail=f"Bundle lookup failed: {exc.msg}")
    return BundleStatusResponse(**out)


@router.delete("/managed-numbers/{phone_number_id}", status_code=204)
async def delete_managed_number(
    phone_number_id: int,
    user: UserModel = Depends(get_user),
):
    """Release a Sysevo-managed number back to Twilio and remove DB records."""
    # Superusers manage numbers across all orgs (matching the configs list view,
    # which also shows all orgs to superusers). Everyone else stays strictly
    # scoped to their own org for tenant isolation.
    org_id = None if user.is_superuser else user.selected_organization_id
    row = await db_client.get_phone_number(phone_number_id, organization_id=org_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found.")

    meta = row.extra_metadata or {}
    if not meta.get("is_managed"):
        raise HTTPException(status_code=400, detail="This number is not a Sysevo-managed number.")

    twilio_sid = meta.get("managed_twilio_sid")

    # Resolved BEFORE the delete, from the row's own config — once the row is
    # gone we can no longer look up which account provisioned it.
    provisioner = await _provisioner_for_number(row) if twilio_sid else None

    # Delete the DB row first so the record is gone even if Twilio release fails.
    await db_client.delete_phone_number(phone_number_id, organization_id=org_id)

    if twilio_sid and provisioner:
        released = await asyncio.to_thread(provisioner.release_number, twilio_sid)
        if not released:
            logger.warning(f"[quick_connect] Failed to release Twilio SID {twilio_sid} for phone_number_id={phone_number_id}")


class ToggleActiveRequest(BaseModel):
    is_active: bool


@router.patch("/managed-numbers/{phone_number_id}")
async def toggle_managed_number(
    phone_number_id: int,
    body: ToggleActiveRequest,
    user: UserModel = Depends(get_user),
):
    """
    Enable/disable routing for a Sysevo-managed number. The inbound dispatcher
    only routes calls for ``is_active`` numbers, so disabling effectively pauses
    forwarding (calls stop reaching the agent) and re-enabling reconnects it —
    without releasing the number.
    """
    # Superusers manage across all orgs; other users stay org-scoped (isolation).
    org_id = None if user.is_superuser else user.selected_organization_id
    row = await db_client.get_phone_number(phone_number_id, organization_id=org_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found.")

    meta = row.extra_metadata or {}
    if not meta.get("is_managed"):
        raise HTTPException(status_code=400, detail="This number is not a Sysevo-managed number.")

    updated = await db_client.update_phone_number(
        phone_number_id,
        telephony_configuration_id=row.telephony_configuration_id,
        is_active=body.is_active,
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to update the number.")
    return {"id": updated.id, "is_active": updated.is_active}


class ManagedNumberBillingRequest(BaseModel):
    phone_number_id: int
    action: Literal["pause", "resume", "release"]


@router.post("/internal/managed-number-billing")
async def managed_number_billing(
    body: ManagedNumberBillingRequest,
    x_sysevo_secret: Optional[str] = Header(None, alias="x-sysevo-secret"),
):
    """
    Internal endpoint for the Supabase rental-billing cron. Secret-authenticated
    (no user scope) so it can pause/resume routing or release a managed number
    on (non-)payment of the monthly fee, regardless of which org it belongs to.
    """
    expected = os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not expected or x_sysevo_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    row = await db_client.get_phone_number(body.phone_number_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found.")

    if body.action in ("pause", "resume"):
        await db_client.update_phone_number(
            body.phone_number_id,
            telephony_configuration_id=row.telephony_configuration_id,
            is_active=(body.action == "resume"),
        )
        return {"ok": True, "action": body.action}

    # release: hand the Twilio number back, then remove the DB row.
    meta = row.extra_metadata or {}
    twilio_sid = meta.get("managed_twilio_sid")
    # Resolved BEFORE the delete, from the row's own config (see delete_managed_number).
    provisioner = await _provisioner_for_number(row) if twilio_sid else None
    await db_client.delete_phone_number(body.phone_number_id)
    if twilio_sid and provisioner:
        await asyncio.to_thread(provisioner.release_number, twilio_sid)
    return {"ok": True, "action": "release"}


class OrgConcurrencyRequest(BaseModel):
    workflow_id: int
    max_concurrent: int


@router.post("/internal/org-concurrency")
async def set_org_concurrency(
    body: OrgConcurrencyRequest,
    x_sysevo_secret: Optional[str] = Header(None, alias="x-sysevo-secret"),
):
    """
    Internal endpoint for the Sysevo billing webhook. Secret-authenticated.

    Sets the organization's CONCURRENT_CALL_LIMIT from the Sysevo plan tier
    (Free 2 / Starter 5 / Growth 10 / Business 20). The org is derived from the
    supplied workflow (a Sysevo agent) and validated by lookup — a webhook
    callback with no user scope, so the org is derived from the payload.
    """
    expected = os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not expected or x_sysevo_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    workflow = await db_client.get_workflow_by_id(body.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Sysevo pushes a large sentinel for "unlimited" plan tiers, so clamp on the
    # way in as well as on read — the stored value should never claim more
    # concurrency than the system can actually run.
    max_concurrent = clamp_to_system_max(body.max_concurrent)

    await db_client.upsert_configuration(
        workflow.organization_id,
        OrganizationConfigurationKey.CONCURRENT_CALL_LIMIT.value,
        {"value": max_concurrent},
    )
    return {
        "ok": True,
        "organization_id": workflow.organization_id,
        "max_concurrent": max_concurrent,
    }
