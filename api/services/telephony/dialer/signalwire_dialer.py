"""SignalWire implementation of the dialer provider.

Mints a Fabric *subscriber* token, which is what the browser SDK registers
with. Verified empirically against the live space:

    POST https://{space}/api/fabric/subscribers/tokens
    HTTP Basic (SIGNALWIRE_PROJECT_ID, SIGNALWIRE_API_TOKEN)
    body {"reference": "<stable string>"}
    -> 200 {"subscriber_id": "...", "token": "..."}

``reference`` is create-or-get, NOT create: a stable string per rep creates
that subscriber once and returns the same one on every later call. That is
why the reference is ``rep-{user_id}`` and must never be randomised - a UUID
per request would mint a brand new subscriber on every page load and the
account would fill up with orphans.

SCOPE: dialer only. Campaigns, the AI-agent pipeline, managed provisioning
and inbound routing stay on Twilio and never reach this module. Notably this
provider is deliberately NOT registered in
``api.services.telephony.registry`` - see signalwire_routes.py.
"""

import os

import httpx
from loguru import logger

from api.services.telephony.dialer.provider import DialerCredentials

# The browser dials this resource address. Overridable so the SignalWire
# resource can be renamed without a redeploy.
_DEFAULT_DESTINATION = "/public/sysevo-dialer?channel=audio"

_TOKEN_PATH = "/api/fabric/subscribers/tokens"


class SignalWireNotConfigured(Exception):
    """Raised when SignalWire credentials are missing, or the token endpoint
    refuses to issue a token.

    Deliberately NOT fail-soft. ``/voice-token`` turns this into a 503 that
    the dialer UI shows the rep. A rep whose token could not be minted has a
    dead dialer either way; the only question is whether they can see why.
    """


def _space_host() -> str:
    """Normalise SIGNALWIRE_SPACE_URL to a bare host.

    The env var is documented as ``sysevo.signalwire.com`` but people paste
    ``https://sysevo.signalwire.com/`` just as often, and the difference
    would otherwise show up as an unhelpful connect error.
    """
    raw = (os.environ.get("SIGNALWIRE_SPACE_URL") or "").strip()
    for scheme in ("https://", "http://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme) :]
            break
    return raw.rstrip("/")


def resolve_dialer_destination() -> str:
    return (
        os.environ.get("SIGNALWIRE_DIALER_DESTINATION") or ""
    ).strip() or _DEFAULT_DESTINATION


class SignalWireDialerProvider:
    name = "signalwire"

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        space = _space_host()
        project_id = (os.environ.get("SIGNALWIRE_PROJECT_ID") or "").strip()
        api_token = (os.environ.get("SIGNALWIRE_API_TOKEN") or "").strip()

        missing = [
            name
            for name, value in (
                ("SIGNALWIRE_SPACE_URL", space),
                ("SIGNALWIRE_PROJECT_ID", project_id),
                ("SIGNALWIRE_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            raise SignalWireNotConfigured(
                "SignalWire dialer is not configured: missing " + ", ".join(missing)
            )

        # Stable per rep - see the module docstring on create-or-get.
        reference = f"rep-{user_id}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{space}{_TOKEN_PATH}",
                    json={"reference": reference},
                    auth=(project_id, api_token),
                    timeout=10.0,
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP/JSON failure is "no token"
            # The exception text can carry the request body but never the
            # credentials (httpx keeps Basic auth in a header it does not
            # echo), and the token itself is only in the RESPONSE, which we
            # do not log.
            logger.error(f"SignalWire token mint failed for {reference}: {exc}")
            raise SignalWireNotConfigured(
                f"SignalWire refused to issue a dialer token for {reference}"
            ) from exc

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token or not isinstance(token, str):
            logger.error(
                f"SignalWire token response for {reference} had no usable token "
                f"(keys: {sorted(payload) if isinstance(payload, dict) else type(payload).__name__})"
            )
            raise SignalWireNotConfigured(
                f"SignalWire returned no token for {reference}"
            )

        return DialerCredentials(
            token=token,
            identity=reference,
            destination=resolve_dialer_destination(),
        )
