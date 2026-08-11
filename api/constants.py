import os
from pathlib import Path

from api.enums import Environment

ENVIRONMENT = os.getenv("ENVIRONMENT", Environment.LOCAL.value)
# Absolute path to the project root directory (i.e. the directory containing
# the top-level api/ package). Having a single canonical location helps
# when constructing file-system paths elsewhere in the codebase.
APP_ROOT_DIR: Path = Path(__file__).resolve().parent

FILLER_SOUND_PROBABILITY = 0.0

VOICEMAIL_RECORDING_DURATION = 5.0

# Langfuse Configuration
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")

# URLs for deployment
BACKEND_API_ENDPOINT = os.getenv("BACKEND_API_ENDPOINT", "http://localhost:8000")
UI_APP_URL = os.getenv("UI_APP_URL", "http://localhost:3010")

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "oss")
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
AUTH_PROVIDER = os.getenv("AUTH_PROVIDER", "local")
DOGRAH_MPS_SECRET_KEY = os.getenv("DOGRAH_MPS_SECRET_KEY", None)
MPS_API_URL = os.getenv("MPS_API_URL", "https://services.dograh.com")

# Supabase auth (used when AUTH_PROVIDER=supabase)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
# Service-role key for server-to-server Supabase reads with no end-user
# session in flight (e.g. resolving a rep's assigned dialer caller ID from
# inside a Twilio webhook) - never forward this to a client.
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Storage Configuration
ENABLE_AWS_S3 = os.getenv("ENABLE_AWS_S3", "false").lower() == "true"

# MinIO Configuration
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "voice-audio")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# AWS S3 Configuration
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

# Sentry configuration
SENTRY_DSN = os.getenv("SENTRY_DSN")

# PostHog configuration
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")


ENABLE_ARI_STASIS = os.getenv("ENABLE_ARI_STASIS", "false").lower() == "true"
SERIALIZE_LOG_OUTPUT = os.getenv("SERIALIZE_LOG_OUTPUT", "false").lower() == "true"

# Logging configuration
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", None)
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

# Log rotation configuration
LOG_ROTATION_SIZE = os.getenv("LOG_ROTATION_SIZE", "100 MB")
LOG_RETENTION = os.getenv("LOG_RETENTION", "7 days")
LOG_COMPRESSION = os.getenv("LOG_COMPRESSION", "gz")
ENABLE_TELEMETRY = os.getenv("ENABLE_TELEMETRY", "false").lower() == "true"


def _get_version() -> str:
    """Read version from pyproject.toml."""
    try:
        import tomllib

        pyproject_path = APP_ROOT_DIR / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)
        return pyproject.get("project", {}).get("version", "dev")
    except Exception:
        return "dev"


# Application version (read from pyproject.toml)
APP_VERSION = _get_version()

# Country code mapping: ISO country code -> international dialing prefix
COUNTRY_CODES = {
    "US": "1",  # United States
    "CA": "1",  # Canada
    "GB": "44",  # United Kingdom
    "IN": "91",  # India
    "AU": "61",  # Australia
    "DE": "49",  # Germany
    "FR": "33",  # France
    "BR": "55",  # Brazil
    "MX": "52",  # Mexico
    "IT": "39",  # Italy
    "ES": "34",  # Spain
    "NL": "31",  # Netherlands
    "SE": "46",  # Sweden
    "NO": "47",  # Norway
    "DK": "45",  # Denmark
    "FI": "358",  # Finland
    "CH": "41",  # Switzerland
    "AT": "43",  # Austria
    "BE": "32",  # Belgium
    "LU": "352",  # Luxembourg
    "IE": "353",  # Ireland
}

DEFAULT_ORG_CONCURRENCY_LIMIT = os.getenv("DEFAULT_ORG_CONCURRENCY_LIMIT", 2)

# Hard ceiling on simultaneous calls PER ORGANISATION. No plan tier, org
# configuration, or campaign setting may exceed it — Sysevo pushes a large
# sentinel for "unlimited" tiers, and this is what stops that authorising more
# concurrent pipelines than the host can actually run.
#
# Set to 20 to match measured capacity of the current box (2026-08-08):
# 4 cores shared with ari_manager/orchestrator/arq/nginx/postgres/redis,
# FASTAPI_WORKERS=2 (so 2 event loops carry every voice pipeline), and ~2,096
# MiB of container headroom above a 1,371 MiB idle footprint. At 20 that is
# 10 pipelines per event loop and ~105 MiB per call. At 50 it would be 25 per
# loop and ~42 MiB per call, which is where audio jitter and OOM risk start.
#
# Raising this is a HARDWARE decision, not a config change: ~8 cores and
# 12-16 GB would be needed for 50. Verify with scripts/ramp_probe.sh before
# moving it, and remember the mirrors — dograh/ui/src/constants/concurrency.ts,
# and on the Sysevo side src/lib/voice/concurrency.ts plus
# supabase/functions/_shared/orgConcurrency.ts (whose test enforces the match).
#
# NOTE this is per-org, not a global cap on the box: N organisations may each
# run up to this many calls concurrently.
MAX_SYSTEM_CONCURRENCY = int(os.getenv("MAX_SYSTEM_CONCURRENCY", 20))

# How many simultaneous calls may share ONE caller ID (CLI).
# 0 (the default) means "no per-CLI cap" — total concurrency is bounded by the
# organisation's CONCURRENT_CALL_LIMIT alone, so a single configured number can
# carry the whole limit. Carriers do not restrict concurrent originations per
# DID; the old one-call-per-number pool was a caller-ID *rotation* strategy that
# silently became a hard concurrency ceiling for orgs with a single number.
# Set a positive value (env, or the per-org CALLS_PER_NUMBER configuration) to
# deliberately spread calls across DIDs.
DEFAULT_CALLS_PER_NUMBER = os.getenv("DEFAULT_CALLS_PER_NUMBER", 0)
DEFAULT_CAMPAIGN_RETRY_CONFIG = {
    "enabled": True,
    "max_retries": 1,
    "retry_delay_seconds": 120,
    "retry_on_busy": True,
    "retry_on_no_answer": True,
    "retry_on_voicemail": False,
}


# Circuit breaker defaults for campaign call failure detection
DEFAULT_CIRCUIT_BREAKER_CONFIG = {
    "enabled": True,
    "failure_threshold": 0.5,  # 50% failure rate trips the breaker
    "window_seconds": 120,  # 2-minute sliding window
    "min_calls_in_window": 5,  # Don't trip until at least 5 outcomes
}


TURN_SECRET = os.getenv("TURN_SECRET")
TURN_HOST = os.getenv("TURN_HOST", "localhost")
TURN_PORT = int(os.getenv("TURN_PORT", "3478"))
TURN_TLS_PORT = int(os.getenv("TURN_TLS_PORT", "5349"))
TURN_CREDENTIAL_TTL = int(os.getenv("TURN_CREDENTIAL_TTL", "86400"))
# Diagnostic flag: when true, strip all non-relay ICE candidates from the
# answer SDP so every media path must traverse the TURN server. Use for
# verifying TURN connectivity end-to-end; expect connection failures if
# TURN is misconfigured or unreachable.
FORCE_TURN_RELAY = os.getenv("FORCE_TURN_RELAY", "false").lower() == "true"

# OSS Email/Password Auth
OSS_JWT_SECRET = os.getenv("OSS_JWT_SECRET", "change-me-in-production")
OSS_JWT_EXPIRY_HOURS = int(os.getenv("OSS_JWT_EXPIRY_HOURS", "720"))  # 30 days

TUNER_BASE_URL = os.getenv("TUNER_BASE_URL", "https://api.usetuner.ai")
