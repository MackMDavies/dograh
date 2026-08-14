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

# Codes worth naming plainly, because the fix is an account action rather
# than anything an engineer can change in this codebase.
_ACCOUNT_LEVEL_HINTS = {
    "insufficient_balance": "the SignalWire account has insufficient balance - top it up in the SignalWire dashboard",
}


def _describe_signalwire_error(response: "httpx.Response") -> str:
    """Turn a SignalWire error response into something a human can act on.

    SignalWire returns machine-readable errors in the BODY:

        {"errors":[{"code":"insufficient_balance",
                    "message":"The account has insufficient balance", ...}]}

    The status line alone is useless - the first time this fired in
    production it read "422 unknown", which took a manual curl to decode
    when the answer was sitting in the body all along. Never returns the
    raw body verbatim: it is bounded and shape-checked, so a surprise
    payload cannot dump something large or sensitive into logs or the UI.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - non-JSON error body
        text = (response.text or "").strip()
        return f"HTTP {response.status_code}" + (f": {text[:200]}" if text else "")

    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list) or not errors:
        return f"HTTP {response.status_code}"

    parts: list[str] = []
    for err in errors[:3]:
        if not isinstance(err, dict):
            continue
        code = str(err.get("code") or "").strip()
        message = str(err.get("message") or "").strip()[:200]
        hint = _ACCOUNT_LEVEL_HINTS.get(code)
        if hint:
            parts.append(hint)
        elif code and message:
            parts.append(f"{message} ({code})")
        elif message or code:
            parts.append(message or code)
    return "; ".join(parts) or f"HTTP {response.status_code}"


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

        # ONE shared subscriber for the whole dialer, deliberately - NOT one
        # per rep.
        #
        # SignalWire bills each subscriber as a monthly resource, and
        # `reference` is create-or-get: a per-rep reference silently
        # provisions a billable resource the first time any rep opens the
        # dialer. That makes the bill scale with headcount for no benefit,
        # and it is a charge that is neither a dial nor a phone number -
        # which is the line we agreed not to cross.
        #
        # Rep attribution does not need separate SignalWire identities: the
        # rep id travels in the dial's user variables and is written to
        # dialer_calls by sw-dialer-connect, so per-rep reporting comes from
        # our own data. `identity` below still returns rep-{user_id} for that
        # purpose - only the BILLABLE SignalWire subscriber is shared.
        #
        # Consequence to keep in mind: every rep's browser authenticates as
        # the same subscriber, so inbound-to-a-specific-rep would not work.
        # The dialer is outbound-only, so that costs us nothing today.
        reference = os.environ.get(
            "SIGNALWIRE_SUBSCRIBER_REFERENCE", "sysevo-dialer"
        ).strip() or "sysevo-dialer"
        identity = f"rep-{user_id}"

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
        except httpx.HTTPStatusError as exc:
            # SignalWire explains itself in the RESPONSE BODY, not the status
            # line. Without this, an account-level problem surfaces as an
            # opaque "422 unknown" and needs a manual curl to decode - which
            # is exactly what happened the first time this fired, for
            # insufficient_balance. Surface their words to the operator AND
            # to the rep, so the UI says what is actually wrong.
            detail = _describe_signalwire_error(exc.response)
            logger.error(
                f"SignalWire token mint failed for {reference} "
                f"(HTTP {exc.response.status_code}): {detail}"
            )
            raise SignalWireNotConfigured(
                f"SignalWire would not issue a dialer token: {detail}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport/JSON failures are "no token"
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
            # The REP's identity, not the shared SignalWire subscriber. This
            # is what the frontend sends back as a user variable and what
            # dialer_calls attributes the call to.
            identity=identity,
            destination=resolve_dialer_destination(),
        )
