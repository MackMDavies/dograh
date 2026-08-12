"""
Superuser endpoints for platform-level managed telephony.
"""
import asyncio
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

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
