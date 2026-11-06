"""SignalWire Compatibility API credentials; separate from the rep dialer."""
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


def normalize_space_url(value: str) -> str:
    value = value.strip().lower()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
        or parsed.path not in ('', '/') or parsed.query or parsed.fragment
        or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.signalwire\.com', host)):
        raise ValueError('Enter your Space hostname, such as yourspace.signalwire.com')
    return host


class SignalWireConfigurationRequest(BaseModel):
    provider: Literal['signalwire'] = 'signalwire'
    space_url: str
    project_id: str
    api_token: str = Field(min_length=1)
    signing_key: str = Field(min_length=1)
    from_numbers: list[str] = Field(default_factory=list)

    @field_validator('space_url')
    @classmethod
    def valid_space(cls, value):
        return normalize_space_url(value)

    @field_validator('project_id')
    @classmethod
    def valid_project(cls, value):
        return str(UUID(value.strip()))


class SignalWireConfigurationResponse(BaseModel):
    provider: Literal['signalwire'] = 'signalwire'
    space_url: str
    project_id: str
    api_token: str
    signing_key: str
    from_numbers: list[str] = Field(default_factory=list)
