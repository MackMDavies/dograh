"""Provider abstraction for the sales-rep dialer.

SCOPE: this abstraction covers the DIALER ONLY. Campaigns, the AI-agent
telephony pipeline, managed number provisioning and inbound routing are
always Twilio and deliberately do not go through here - see
docs/superpowers/specs/2026-08-13-dialer-signalwire-provider-design.md.
"""

from dataclasses import dataclass
from typing import Protocol


class UnknownDialerProvider(Exception):
    """Raised for a provider name that has no implementation."""


@dataclass(frozen=True)
class DialerCredentials:
    """What the browser needs in order to place calls.

    ``destination`` is provider-specific: Twilio ignores it (the TwiML App's
    Voice URL is fixed server-side), while SignalWire dials it as a resource
    address. It is returned for both so the frontend has one shape to handle.
    """

    token: str
    identity: str
    destination: str


class DialerProvider(Protocol):
    name: str

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        """Issue short-lived browser credentials for this rep."""


def get_dialer_provider(name: str) -> DialerProvider:
    normalized = (name or "").strip().lower()

    # Imported here, not at module scope, so that importing the interface
    # never drags in either provider's SDK.
    if normalized == "twilio":
        from api.services.telephony.dialer.twilio_dialer import TwilioDialerProvider

        return TwilioDialerProvider()
    if normalized == "signalwire":
        from api.services.telephony.dialer.signalwire_dialer import (
            SignalWireDialerProvider,
        )

        return SignalWireDialerProvider()
    raise UnknownDialerProvider(f"No dialer provider implementation for {name!r}")


def resolve_active_dialer_provider() -> str:
    """Which provider the dialer currently runs on.

    Env-based for now so it can be changed without a migration; the spec's
    'platform setting' can replace this later without touching callers.
    Defaults to twilio so an unset value preserves today's behaviour.
    """
    import os

    return (os.environ.get("SYSEVO_DIALER_PROVIDER") or "twilio").strip().lower()
