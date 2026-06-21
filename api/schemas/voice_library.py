"""Pydantic schemas for voice library operations."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class VoiceLibraryCreateSchema(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False
    language: Optional[str] = None
    accent: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    use_case: Optional[str] = None


class VoiceLibraryUpdateSchema(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    language: Optional[str] = None
    accent: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    use_case: Optional[str] = None


class VoiceLibraryResponseSchema(BaseModel):
    uuid: str
    user_id: int
    organization_id: int
    name: str
    description: Optional[str] = None
    provider: str
    provider_voice_id: Optional[str] = None
    language: Optional[str] = None
    accent: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    use_case: Optional[str] = None
    is_public: bool
    status: str
    audio_preview_url: Optional[str] = None
    labels: dict = {}
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ElevenLabsCatalogVoiceSchema(BaseModel):
    voice_id: str
    name: str
    preview_url: Optional[str] = None
    labels: dict = {}
    category: Optional[str] = None


class ElevenLabsImportRequestSchema(BaseModel):
    voice_ids: list[str]
    is_public: bool = True


class ElevenLabsSharedVoiceSchema(BaseModel):
    voice_id: str
    name: str
    preview_url: Optional[str] = None
    labels: dict = {}
    category: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    gender: Optional[str] = None
    use_case: Optional[str] = None
    accent: Optional[str] = None
    age: Optional[str] = None


class GoogleTTSCatalogVoiceSchema(BaseModel):
    name: str
    gender: Optional[str] = None
    language_codes: list[str] = []


class GoogleTTSImportRequestSchema(BaseModel):
    voice_names: list[str]
    is_public: bool = True
