"""Asterisk ARI provider; shared media operations also serve managed SIP trunks."""
from api.services.telephony.asterisk_media import AsteriskMediaProvider as ARIProvider

__all__ = ["ARIProvider"]
