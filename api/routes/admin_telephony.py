"""
Superuser endpoints for platform-level managed telephony.
"""
import asyncio
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from api.db import db_client
from api.db.models import (
    OrganizationModel,
    TelephonyConfigurationModel,
    TelephonyPhoneNumberModel,
    UserModel,
    WorkflowModel,
)
from api.services.auth.depends import get_superuser

router = APIRouter(prefix="/admin/telephony", tags=["admin-telephony"])


class ManagedStatusResponse(BaseModel):
    configured: bool
    account_sid_preview: Optional[str]  # e.g. "AC12ab****" or None
    source: Optional[Literal["database", "environment"]] = None


class SaveTwilioCredentialsRequest(BaseModel):
    account_sid: str
    auth_token: str


class SaveTwilioCredentialsResponse(BaseModel):
    configured: bool
    account_sid_preview: Optional[str]
    source: Literal["database"]
    friendly_name: Optional[str] = None


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


class ManagedNumbersResponse(BaseModel):
    numbers: list[ManagedNumberItem]
    total: int


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


@router.post("/twilio-credentials", response_model=SaveTwilioCredentialsResponse)
async def save_twilio_credentials(
    body: SaveTwilioCredentialsRequest,
    _user: UserModel = Depends(get_superuser),
):
    """
    Validate and store platform-level Twilio credentials (auth token encrypted
    at rest). Credentials are verified against Twilio before saving so a bad
    SID/token is rejected up front.
    """
    account_sid = body.account_sid.strip()
    auth_token = body.auth_token.strip()
    if not account_sid or not auth_token:
        raise HTTPException(status_code=422, detail="account_sid and auth_token are required.")
    if not account_sid.startswith("AC"):
        raise HTTPException(status_code=422, detail="account_sid should start with 'AC'.")

    # Validate by fetching the account — a cheap authenticated call.
    def _validate() -> str:
        client = Client(account_sid, auth_token)
        account = client.api.accounts(account_sid).fetch()
        return account.friendly_name or account_sid

    try:
        friendly_name = await asyncio.to_thread(_validate)
    except TwilioRestException as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Twilio rejected these credentials: {exc.msg}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[admin_telephony] Credential validation failed: {exc}")
        raise HTTPException(status_code=400, detail="Could not validate credentials with Twilio.")

    await db_client.save_platform_twilio_credentials(account_sid, auth_token)
    logger.info(f"[admin_telephony] Platform Twilio credentials saved (sid={_mask_sid(account_sid)})")
    return SaveTwilioCredentialsResponse(
        configured=True,
        account_sid_preview=_mask_sid(account_sid),
        source="database",
        friendly_name=friendly_name,
    )


@router.delete("/twilio-credentials", response_model=ManagedStatusResponse)
async def delete_twilio_credentials(_user: UserModel = Depends(get_superuser)):
    """
    Remove DB-stored platform Twilio credentials, reverting to the env-var
    fallback (if any).
    """
    await db_client.clear_platform_twilio_credentials()
    sid, source = await _resolve_platform_sid()
    return ManagedStatusResponse(
        configured=sid is not None,
        account_sid_preview=_mask_sid(sid),
        source=source,
    )


@router.get("/numbers", response_model=ManagedNumbersResponse)
async def list_managed_numbers(_user: UserModel = Depends(get_superuser)):
    """List all Sysevo-managed phone numbers across every org."""
    async with db_client.async_session() as session:
        stmt = (
            select(TelephonyPhoneNumberModel, OrganizationModel, WorkflowModel)
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
                TelephonyPhoneNumberModel.extra_metadata["is_managed"].astext == "true"
            )
            .order_by(TelephonyPhoneNumberModel.id.desc())
        )
        result = await session.execute(stmt)
        rows = result.all()

    items = []
    for num, org, workflow in rows:
        meta = num.extra_metadata or {}
        raw_sid = meta.get("managed_twilio_sid", "")
        sid_preview = (raw_sid[:6] + "****") if raw_sid else None
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
            )
        )

    return ManagedNumbersResponse(numbers=items, total=len(items))
