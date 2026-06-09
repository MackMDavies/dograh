"""Pydantic schemas for AI provider management endpoints."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ProviderConnectionResponseSchema(BaseModel):
    id: int
    organization_id: int
    service_type: str
    provider: str
    display_name: Optional[str] = None
    api_key_masked: Optional[str] = None   # last 4 chars only
    extra_config: Dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    model_count: int = 0


class CreateProviderConnectionSchema(BaseModel):
    service_type: str   # llm | tts | stt | embeddings | realtime
    provider: str
    api_key: Optional[str] = None
    extra_config: Dict[str, Any] = {}
    display_name: Optional[str] = None


class UpdateProviderConnectionSchema(BaseModel):
    api_key: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None
    display_name: Optional[str] = None


class AvailableModelResponseSchema(BaseModel):
    id: int
    connection_id: int
    organization_id: int
    service_type: str
    provider: str              # denormalized from connection for frontend convenience
    model_id: str
    display_name: Optional[str] = None
    is_client_available: bool
    is_default: bool
    cost_per_min_usd: Optional[float] = None
    native_cost_display: Optional[str] = None
    our_price_per_min_usd: Optional[float] = None


class SetModelClientAvailableSchema(BaseModel):
    is_client_available: bool


class SetModelOurPriceSchema(BaseModel):
    our_price_per_min_usd: Optional[float] = None


class ApplyMarginSchema(BaseModel):
    margin_percent: float
    connection_id: Optional[int] = None


class SetModelDisplayNameSchema(BaseModel):
    display_name: Optional[str] = None
