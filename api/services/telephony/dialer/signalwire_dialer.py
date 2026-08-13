"""SignalWire implementation of the dialer provider. Filled in by Task 5."""

from api.services.telephony.dialer.provider import DialerCredentials


class SignalWireDialerProvider:
    name = "signalwire"

    async def mint_credentials(self, *, user_id: int) -> DialerCredentials:
        raise NotImplementedError("Implemented in Task 5")
