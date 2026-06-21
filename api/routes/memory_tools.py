"""Memory and calendar tools provisioning for Dograh voice agents.

Creates pre-configured http_api tools that call the Sysevo Supabase
agent-memory-lookup function, scoped to a specific client account.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from api.constants import SUPABASE_ANON_KEY, SUPABASE_URL
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user

router = APIRouter(prefix="/memory-tools", tags=["memory-tools"])

_EDGE_FN_BASE = f"{SUPABASE_URL}/functions/v1/agent-memory-lookup"

_TOOL_SPECS = [
    {
        "name": "MemoryLookup",
        "description": (
            "Look up a caller's full history by phone number. "
            "Call this immediately when you have the caller's phone number. "
            "Returns name, previous call summaries, preferences, and upcoming bookings."
        ),
        "action": "lookup",
        "icon": "brain",
        "icon_color": "#8b5cf6",
        "parameters": [
            {
                "name": "phone",
                "type": "string",
                "description": "The caller's phone number including country code (e.g. +44...)",
                "required": True,
            }
        ],
    },
    {
        "name": "CustomerSearch",
        "description": (
            "Search for a customer by name, email, phone, or booking reference. "
            "Use when the caller mentions their name or a reference number."
        ),
        "action": "search",
        "icon": "search",
        "icon_color": "#3b82f6",
        "parameters": [
            {
                "name": "search_query",
                "type": "string",
                "description": "Name, email, phone number, or booking reference to search for",
                "required": True,
            }
        ],
    },
    {
        "name": "SaveMemory",
        "description": (
            "Save a summary and outcome for this call to the caller's permanent memory. "
            "Call at the end of every call to retain what was discussed."
        ),
        "action": "save",
        "icon": "save",
        "icon_color": "#10b981",
        "parameters": [
            {"name": "phone_number", "type": "string", "description": "Caller's phone number", "required": True},
            {"name": "caller_name", "type": "string", "description": "Caller's name if known", "required": False},
            {"name": "call_summary", "type": "string", "description": "Brief summary of the call", "required": False},
            {"name": "outcome", "type": "string", "description": "Call outcome (e.g. booking_made, enquiry, callback_requested)", "required": False},
            {"name": "sentiment", "type": "string", "description": "Caller sentiment (positive, neutral, negative)", "required": False},
            {"name": "notes_update", "type": "string", "description": "Any notes to save about this caller", "required": False},
        ],
    },
    {
        "name": "CheckAvailability",
        "description": (
            "Check available appointment slots for a given date. "
            "Returns a list of available times. Use before booking."
        ),
        "action": "check_availability",
        "icon": "calendar",
        "icon_color": "#f59e0b",
        "parameters": [
            {"name": "date", "type": "string", "description": "Date to check (YYYY-MM-DD, or 'today', 'tomorrow')", "required": True},
            {"name": "duration", "type": "number", "description": "Appointment duration in minutes (30 or 60)", "required": False},
            {"name": "customer_name", "type": "string", "description": "Customer's name (optional, for context)", "required": False},
        ],
    },
    {
        "name": "BookAppointment",
        "description": (
            "Book a new appointment for the caller. "
            "Always check availability first, then confirm details with the caller before booking."
        ),
        "action": "book_appointment",
        "icon": "calendar-plus",
        "icon_color": "#f59e0b",
        "parameters": [
            {"name": "date", "type": "string", "description": "Appointment date (YYYY-MM-DD)", "required": True},
            {"name": "time", "type": "string", "description": "Appointment time (HH:MM, 24h format)", "required": True},
            {"name": "customer_name", "type": "string", "description": "Customer's full name", "required": True},
            {"name": "customer_phone", "type": "string", "description": "Customer's phone number", "required": False},
            {"name": "customer_email", "type": "string", "description": "Customer's email address", "required": False},
            {"name": "duration", "type": "number", "description": "Duration in minutes (30 or 60)", "required": False},
            {"name": "title", "type": "string", "description": "Appointment title or type", "required": False},
            {"name": "description", "type": "string", "description": "Additional notes about the appointment", "required": False},
        ],
    },
    {
        "name": "CancelAppointment",
        "description": (
            "Cancel an existing appointment. "
            "Confirm with the caller before cancelling."
        ),
        "action": "cancel_appointment",
        "icon": "calendar-x",
        "icon_color": "#ef4444",
        "parameters": [
            {"name": "customer_phone", "type": "string", "description": "Customer's phone number", "required": False},
            {"name": "customer_name", "type": "string", "description": "Customer's name", "required": False},
            {"name": "date", "type": "string", "description": "Date of the appointment to cancel (YYYY-MM-DD)", "required": False},
            {"name": "time", "type": "string", "description": "Time of the appointment to cancel (HH:MM)", "required": False},
        ],
    },
    {
        "name": "UpdateAppointment",
        "description": (
            "Reschedule or update an existing appointment to a new date and time. "
            "Confirm the new slot with the caller before updating."
        ),
        "action": "update_appointment",
        "icon": "calendar-days",
        "icon_color": "#f59e0b",
        "parameters": [
            {"name": "new_date", "type": "string", "description": "New appointment date (YYYY-MM-DD)", "required": True},
            {"name": "new_time", "type": "string", "description": "New appointment time (HH:MM, 24h format)", "required": True},
            {"name": "customer_phone", "type": "string", "description": "Customer's phone number", "required": False},
            {"name": "customer_name", "type": "string", "description": "Customer's name", "required": False},
            {"name": "original_date", "type": "string", "description": "Original date of the appointment to identify it", "required": False},
        ],
    },
]


def _build_tool_definition(action: str, parameters: list, client_account_id: str) -> dict:
    auth_key = SUPABASE_ANON_KEY or os.getenv("INTERNAL_API_SECRET", "")
    return {
        "schema_version": 1,
        "type": "http_api",
        "config": {
            "method": "POST",
            "url": f"{_EDGE_FN_BASE}?action={action}",
            "headers": {
                "Authorization": f"Bearer {auth_key}",
                "Content-Type": "application/json",
            },
            "parameters": parameters,
            "preset_parameters": [
                {
                    "name": "client_account_id",
                    "type": "string",
                    "value_template": client_account_id,
                    "required": True,
                }
            ],
            "timeout_ms": 8000,
            "customMessage": "Let me check that for you.",
        },
    }


class ProvisionRequest(BaseModel):
    client_account_id: str
    replace_existing: Optional[bool] = False
    tool_names: Optional[list[str]] = None


class ProvisionResponse(BaseModel):
    success: bool
    tools_created: int
    tool_uuids: list[str]


@router.post("/provision", response_model=ProvisionResponse)
async def provision_memory_tools(
    body: ProvisionRequest,
    user: UserModel = Depends(get_user),
):
    """Create the 7 standard memory & calendar tools in the org's tool library.

    Tools are http_api webhooks that call the Sysevo agent-memory-lookup
    Supabase function, scoped to the supplied client_account_id.
    """
    if not body.client_account_id:
        raise HTTPException(status_code=400, detail="client_account_id is required")

    org_id = user.selected_organization_id

    selected_names = set(body.tool_names) if body.tool_names else {s["name"] for s in _TOOL_SPECS}
    specs_to_create = [s for s in _TOOL_SPECS if s["name"] in selected_names]

    if body.replace_existing:
        existing = await db_client.get_tools_for_organization(org_id, status="active", category="http_api")
        for t in existing:
            if t.name in selected_names:
                await db_client.archive_tool(t.tool_uuid, org_id)
        logger.info(f"Archived existing memory tools for org {org_id}")

    created_uuids: list[str] = []
    for spec in specs_to_create:
        definition = _build_tool_definition(spec["action"], spec["parameters"], body.client_account_id)
        tool = await db_client.create_tool(
            organization_id=org_id,
            user_id=user.id,
            name=spec["name"],
            description=spec["description"],
            category="http_api",
            definition=definition,
            icon=spec.get("icon"),
            icon_color=spec.get("icon_color"),
        )
        created_uuids.append(tool.tool_uuid)
        logger.info(f"Created memory tool '{spec['name']}' ({tool.tool_uuid}) for org {org_id}")

    return ProvisionResponse(success=True, tools_created=len(created_uuids), tool_uuids=created_uuids)
