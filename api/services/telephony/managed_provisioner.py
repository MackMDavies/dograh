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


class RegulationNotAutomatable(Exception):
    """The country's regulation needs something we cannot supply automatically (e.g. an
    uploaded document). The message names the requirements, for a person to handle."""


# End-user fields we can fill from a client's KYC, by Twilio attribute name. Anything a
# regulation asks for that is NOT here is reported as missing rather than guessed.
def _end_user_attributes(kyc: Dict[str, Any]) -> Dict[str, str]:
    business = kyc.get("business") or {}
    rep = kyc.get("representative") or {}
    return {
        "business_name": business.get("name") or "",
        "business_registration_identifier": business.get("registration_identifier") or "",
        "business_registration_number": business.get("registration_number") or "",
        "business_website": business.get("website") or "",
        "first_name": rep.get("first_name") or "",
        "last_name": rep.get("last_name") or "",
        "phone_number": rep.get("phone") or "",
        "email": rep.get("email") or "",
        # The bundle is the client's own: they are Twilio's direct customer's customer,
        # and the number is assigned to them.
        "business_identity": "DIRECT_CUSTOMER",
        "is_subassigned": "YES",
    }


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
        self,
        e164: str,
        voice_url: str,
        address_sid: Optional[str] = None,
        bundle_sid: Optional[str] = None,
    ) -> ProvisionedNumber:
        """
        Purchase *e164* from the platform Twilio account and wire its inbound
        webhook to *voice_url*. Pass *address_sid* / *bundle_sid* for countries
        whose regulations require a registered Address and/or an approved
        Regulatory Bundle (e.g. GB and most of the EU). Raises TwilioRestException
        on failure.
        """
        kwargs: Dict[str, Any] = {
            "phone_number": e164,
            "voice_url": voice_url,
            "voice_method": "POST",
        }
        if address_sid:
            kwargs["address_sid"] = address_sid
        if bundle_sid:
            kwargs["bundle_sid"] = bundle_sid
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

    def get_bundle_sid(self, iso_country: str) -> Optional[str]:
        """
        Return an approved Regulatory Bundle SID for *iso_country*, or None.
        Regulated countries (GB, most of the EU) require an approved Bundle to
        buy local numbers. When the platform account has a twilio-approved bundle
        for the country, we attach it automatically so the number provisions
        in-country (instead of falling back to a US line).
        """
        try:
            bundles = self._client.numbers.v2.regulatory_compliance.bundles.list(
                iso_country=iso_country, status="twilio-approved", limit=1
            )
            return bundles[0].sid if bundles else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[managed_provisioner] Bundle lookup failed for {iso_country}: {exc}"
            )
            return None

    # ── Regulatory bundles (in-country numbers) ──────────────────────────────────
    #
    # Twilio refuses a local number in a regulated country (GB, most of the EU) without an
    # approved Regulatory Bundle, created IN THE ACCOUNT THAT BUYS THE NUMBER (end users and
    # documents cannot be shared across accounts). Each client's bundle is built from their
    # own KYC; nothing here is platform-wide.

    def _regulation(self, country: str) -> Optional[Any]:
        found = self._client.numbers.v2.regulatory_compliance.regulations.list(
            iso_country=country.upper(),
            number_type="local",
            end_user_type="business",
            include_constraints=True,
        )
        return found[0] if found else None

    def regulation_for(self, country: str) -> Dict[str, Any]:
        """Whether a local business number in *country* needs a bundle, and which regulation."""
        reg = self._regulation(country)
        if reg is None:
            return {"sid": None, "requires_bundle": False}
        req = reg.requirements or {}
        needs = bool(req.get("end_user")) or any(group for group in (req.get("supporting_document") or []))
        return {"sid": reg.sid, "requires_bundle": needs}

    def create_bundle(
        self,
        country: str,
        kyc: Dict[str, Any],
        email: str,
        status_callback: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build and submit a bundle for one client: Address -> Bundle -> EndUser ->
        SupportingDocument(address) -> assignments -> evaluation -> pending-review.

        Returns {bundle_sid, address_sid, status, failures}. A bundle Twilio's own
        evaluation calls noncompliant is left in draft with the failures, not submitted.
        Raises ValueError naming missing KYC fields BEFORE creating anything, and
        RegulationNotAutomatable when the regulation needs an uploaded file.
        """
        country = country.upper()
        reg = self._regulation(country)
        if reg is None:
            raise ValueError(f"no regulation for local business numbers in {country}")
        req = reg.requirements or {}

        # Which supporting documents we can satisfy: only address-based ones.
        doc_types: List[str] = []
        unmet: List[str] = []
        for group in req.get("supporting_document") or []:
            usable = [d for d in (group[0].get("accepted_documents") or []) if "address_sids" in (d.get("fields") or [])] if group else []
            if usable:
                doc_types.append(usable[0]["type"])
            elif group:
                unmet.append(group[0].get("name") or group[0].get("requirement_name") or "a supporting document")
        if unmet:
            raise RegulationNotAutomatable(
                f"{country} needs documents we cannot supply automatically: {', '.join(unmet)}"
            )

        wanted: List[str] = []
        for end_user in req.get("end_user") or []:
            wanted.extend(end_user.get("fields") or [])
        available = _end_user_attributes(kyc)
        missing = [f for f in wanted if not available.get(f)]
        if missing:
            raise ValueError(f"KYC is missing fields {country} requires: {', '.join(missing)}")
        attributes = {f: available[f] for f in wanted}

        address = kyc.get("address") or {}
        street = ", ".join(x for x in [address.get("street"), address.get("street2")] if x)
        rc = self._client.numbers.v2.regulatory_compliance
        created_address = self._client.addresses.create(
            customer_name=available["business_name"],
            street=street,
            city=address.get("city") or "",
            region=address.get("region") or "",
            postal_code=address.get("postal_code") or "",
            iso_country=(address.get("country") or country).upper(),
            friendly_name=available["business_name"][:64],
        )
        bundle = rc.bundles.create(
            friendly_name=f"{available['business_name']} ({country} local)"[:64],
            email=email,
            regulation_sid=reg.sid,
            **({"status_callback": status_callback} if status_callback else {}),
        )
        end_user = rc.end_users.create(
            friendly_name=available["business_name"][:64], type="business", attributes=attributes,
        )
        documents = [
            rc.supporting_documents.create(
                friendly_name=f"{available['business_name']} address"[:64],
                type=doc_type,
                attributes={"address_sids": [created_address.sid]},
            )
            for doc_type in doc_types
        ]
        handle = rc.bundles(bundle.sid)
        for item in [end_user, *documents]:
            handle.item_assignments.create(object_sid=item.sid)

        evaluation = handle.evaluations.create()
        if getattr(evaluation, "status", None) != "compliant":
            return {
                "bundle_sid": bundle.sid,
                "address_sid": created_address.sid,
                "status": "draft",
                "failures": list(getattr(evaluation, "results", None) or []),
            }
        updated = handle.update(status="pending-review")
        return {
            "bundle_sid": bundle.sid,
            "address_sid": created_address.sid,
            "status": getattr(updated, "status", "pending-review"),
            "failures": [],
        }

    def get_bundle(self, bundle_sid: str) -> Dict[str, Any]:
        """A bundle's status and country. A bundle from another account 404s (TwilioRestException)."""
        rc = self._client.numbers.v2.regulatory_compliance
        bundle = rc.bundles(bundle_sid).fetch()
        iso_country = None
        if getattr(bundle, "regulation_sid", None):
            iso_country = getattr(rc.regulations(bundle.regulation_sid).fetch(), "iso_country", None)
        failures: List[Any] = []
        if bundle.status == "twilio-rejected":
            try:
                latest = rc.bundles(bundle_sid).evaluations.list(limit=1)
                failures = list(latest[0].results or []) if latest else []
            except TwilioRestException:
                failures = []
        return {
            "sid": bundle.sid,
            "status": bundle.status,
            "iso_country": iso_country,
            "valid_until": str(bundle.valid_until) if getattr(bundle, "valid_until", None) else None,
            "failures": failures,
        }

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


async def get_managed_provisioner(
    account_id: Optional[int] = None,
) -> Optional[ManagedProvisioner]:
    """
    Return a ManagedProvisioner if platform credentials are configured,
    otherwise None. Routes call this and return 503 when None.

    If *account_id* is given, resolve that specific stored account instead of
    "the active one" — lets a superuser choose which platform Twilio account
    to buy a number under. Returns None if that id doesn't exist (callers
    should 404, not silently fall back to a different account).

    Otherwise, resolution order: the active DB-stored account first, then the
    SYSEVO_TWILIO_* environment variables as a fallback. The DB is read on
    every call (no per-worker caching) so a credential saved on one worker
    takes effect across all workers immediately.
    """
    # Imported lazily to avoid an import cycle (db_client pulls in many models).
    from api.db import db_client

    if account_id is not None:
        creds = await db_client.get_platform_twilio_credentials_by_id(account_id)
        if not creds:
            return None
        return ManagedProvisioner(account_sid=creds["account_sid"], auth_token=creds["auth_token"])

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
