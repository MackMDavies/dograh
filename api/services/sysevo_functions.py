"""Where Sysevo's edge functions live, for hooks that have no URL of their own.

Every Sysevo hook Dograh calls is an edge function on the same Supabase project, and
the container already carries several of their URLs (SYSEVO_PRE_CALL_CHECK_URL is
required for billing and gating, so it is always there). A new hook is found next to
them, so shipping one needs no .env + compose edits on the box. An explicit env var
still wins.
"""

from __future__ import annotations

import os
from typing import Optional

_KNOWN_HOOK_ENVS = (
    "SYSEVO_PRE_CALL_CHECK_URL",
    "SYSEVO_CALL_LIVE_URL",
    "SYSEVO_MEMORY_PRE_CALL_URL",
    "SYSEVO_WALLET_DEBIT_URL",
)
_MARKER = "/functions/v1/"


def sysevo_function_url(name: str, explicit_env: Optional[str] = None) -> Optional[str]:
    if explicit_env and os.getenv(explicit_env):
        return os.getenv(explicit_env)
    for env in _KNOWN_HOOK_ENVS:
        url = os.getenv(env) or ""
        if _MARKER in url:
            return url.split(_MARKER, 1)[0] + _MARKER + name
    return None
