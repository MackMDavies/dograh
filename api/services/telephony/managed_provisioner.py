"""
Sysevo platform-level Twilio provisioner.

Uses the SYSEVO_TWILIO_ACCOUNT_SID / SYSEVO_TWILIO_AUTH_TOKEN environment
variables to provision and release phone numbers on behalf of any org.
Credentials are never stored in the database — they live in the environment.
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger
from phonenumbers import parse as ph_parse, region_code_for_number
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client


@dataclass
class ProvisionedNumber:
    e164: str
    twilio_sid: str


class ManagedProvisioner:
    def __init__(self, account_sid: str, auth_token: str) -> None:
        # Retained so callers can persist the resolved account_sid on the managed
        # telephony config (needed for inbound webhook matching + signature
        # verification) regardless of whether creds came from DB or env.
        self.account_sid = account_sid
        self.auth_token = auth_token
        self._client = Client(account_sid, auth_token)

    # ── Carrier lookup ────────────────────────────────────────────────────────

    def lookup_carrier(self, number: str) -> Dict[str, Any]:
        """
        Return carrier name, country code, and line type for *number*.
        Falls back gracefully: if Twilio Lookup fails, derives country from the
        number itself (phonenumbers lib) and returns carrier=None.
        """
        country = self._country_from_number(number)
        try:
            result = self._client.lookups.v2.phone_numbers(number).fetch(
                fields="line_type_intelligence"
            )
            lti = result.line_type_intelligence or {}
            return {
                "carrier": lti.get("carrier_name"),
                "country": result.country_code or country,
                "line_type": lti.get("type", "unknown"),
            }
        except Exception as exc:
            logger.warning(f"[managed_provisioner] Lookup failed for {number}: {exc}")
            return {"carrier": None, "country": country, "line_type": "unknown"}

    # ── Number search ─────────────────────────────────────────────────────────

    def search_available_numbers(
        self, country: str, area_code: Optional[str] = None, limit: int = 8
    ) -> List[str]:
        """Return up to *limit* purchasable E.164 numbers in *country*."""
        try:
            kwargs: Dict[str, Any] = {"limit": limit}
            if area_code:
                kwargs["area_code"] = area_code
            numbers = self._client.available_phone_numbers(country).local.list(**kwargs)
            return [n.phone_number for n in numbers]
        except TwilioRestException as exc:
            logger.error(f"[managed_provisioner] Number search failed: {exc}")
            return []

    # ── Provisioning ──────────────────────────────────────────────────────────

    def provision_number(
        self, e164: str, voice_url: str, address_sid: Optional[str] = None
    ) -> ProvisionedNumber:
        """
        Purchase *e164* from the platform Twilio account and wire its inbound
        webhook to *voice_url*. Pass *address_sid* for countries whose regulations
        require a registered address (e.g. GB). Raises TwilioRestException on failure.
        """
        kwargs: Dict[str, Any] = {
            "phone_number": e164,
            "voice_url": voice_url,
            "voice_method": "POST",
        }
        if address_sid:
            kwargs["address_sid"] = address_sid
        purchased = self._client.incoming_phone_numbers.create(**kwargs)
        logger.info(f"[managed_provisioner] Provisioned {e164} → SID {purchased.sid}")
        return ProvisionedNumber(e164=purchased.phone_number, twilio_sid=purchased.sid)

    def get_address_sid(self, iso_country: str) -> Optional[str]:
        """
        Return the SID of a registered Twilio Address for *iso_country*, or None.
        Some countries require an address to buy local numbers; if the platform
        account has one registered, we attach it automatically.
        """
        try:
            addresses = self._client.addresses.list(
                iso_country=iso_country, limit=1
            )
            return addresses[0].sid if addresses else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[managed_provisioner] Address lookup failed for {iso_country}: {exc}"
            )
            return None

    def release_number(self, twilio_sid: str) -> bool:
        """Release a number back to Twilio. Returns True on success."""
        try:
            self._client.incoming_phone_numbers(twilio_sid).delete()
            logger.info(f"[managed_provisioner] Released SID {twilio_sid}")
            return True
        except TwilioRestException as exc:
            logger.error(f"[managed_provisioner] Release failed for {twilio_sid}: {exc}")
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _country_from_number(number: str) -> str:
        try:
            parsed = ph_parse(number)
            return region_code_for_number(parsed) or "US"
        except Exception:
            return "US"


def _provisioner_from_env() -> Optional[ManagedProvisioner]:
    sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    token = os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return None
    return ManagedProvisioner(account_sid=sid, auth_token=token)


async def get_managed_provisioner() -> Optional[ManagedProvisioner]:
    """
    Return a ManagedProvisioner if platform credentials are configured,
    otherwise None. Routes call this and return 503 when None.

    Resolution order: DB-stored credentials (set via the admin UI) first, then
    the SYSEVO_TWILIO_* environment variables as a fallback. The DB is read on
    every call (no per-worker caching) so a credential saved on one worker takes
    effect across all workers immediately.
    """
    # Imported lazily to avoid an import cycle (db_client pulls in many models).
    from api.db import db_client

    try:
        creds = await db_client.get_platform_twilio_credentials()
        if creds:
            return ManagedProvisioner(
                account_sid=creds["account_sid"], auth_token=creds["auth_token"]
            )
    except Exception as exc:  # noqa: BLE001 — never block provisioning on DB hiccups
        logger.warning(
            f"[managed_provisioner] DB credential load failed, "
            f"falling back to env: {exc}"
        )
    return _provisioner_from_env()
