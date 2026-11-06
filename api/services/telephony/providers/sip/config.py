"""Registered SIP trunks use the platform's private Asterisk media gateway."""
import re
from typing import Literal
from urllib.parse import urlsplit
import phonenumbers
from pydantic import BaseModel, Field, field_validator


def sip_host(value: str) -> str:
    parsed = urlsplit('sip://' + value.strip())
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        or not re.fullmatch(r'[a-zA-Z0-9.-]+', parsed.hostname) or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
        raise ValueError('Enter a SIP hostname, optionally followed by its port.')
    return parsed.netloc.lower()


class SIPConfigurationRequest(BaseModel):
    provider: Literal['sip'] = 'sip'
    domain: str
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)
    number: str
    from_numbers: list[str] = Field(default_factory=list)

    @field_validator('domain')
    @classmethod
    def valid_host(cls, value):
        return sip_host(value)

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        if not re.fullmatch(r'[a-zA-Z0-9+_.-]+', value):
            raise ValueError('Enter the SIP account username or extension.')
        return value

    @field_validator('password')
    @classmethod
    def valid_password(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('SIP passwords cannot contain line breaks or control characters.')
        return value

    @field_validator('number')
    @classmethod
    def valid_number(cls, value):
        try:
            parsed = phonenumbers.parse(value, None)
        except phonenumbers.NumberParseException:
            raise ValueError('Enter the United States phone number routed to this trunk.') from None
        if not phonenumbers.is_valid_number(parsed) or phonenumbers.region_code_for_number(parsed) != 'US':
            raise ValueError('Enter the United States phone number routed to this trunk.')
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class SIPConfigurationResponse(BaseModel):
    provider: Literal['sip'] = 'sip'
    domain: str
    username: str
    password: str
    number: str
