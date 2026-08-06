"""ARQ worker configuration - setup logging before importing tasks"""

import ssl
from urllib.parse import urlparse

from loguru import logger

from api.constants import REDIS_URL

# Setup logging - this is now idempotent and safe to call multiple times
from api.logging_config import setup_logging
from api.tasks.function_names import FunctionNames

setup_logging()

# Now import ARQ and task dependencies
from arq import create_pool, cron
from arq.connections import ArqRedis, RedisSettings

parsed_url = urlparse(REDIS_URL)

# Check if we're using TLS (rediss://)
use_ssl = parsed_url.scheme == "rediss"

# Create SSL context if using rediss://
ssl_context = None
if use_ssl:
    ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

REDIS_SETTINGS = RedisSettings(
    host=parsed_url.hostname or "localhost",
    port=parsed_url.port or 6379,
    password=parsed_url.password,
    conn_timeout=10,
    ssl=use_ssl,
    ssl_ca_certs=None if not use_ssl else None,
    ssl_certfile=None,
    ssl_keyfile=None,
    ssl_check_hostname=False if use_ssl else None,
)

from api.tasks.campaign_tasks import (
    process_campaign_batch,
    sync_campaign_source,
)
from api.tasks.knowledge_base_processing import process_knowledge_base_document
from api.tasks.run_integrations import run_integrations_post_workflow_run
from api.tasks.s3_upload import (
    process_workflow_completion,
    upload_voicemail_audio_to_s3,
)
from api.tasks.wallet_reconciliation import reconcile_wallet_debits
from api.tasks.memory_reconciliation import reconcile_memory
from api.tasks.telephony_cost_reconciliation import reconcile_telephony_cost
from api.services.api_usage_counter import flush_api_request_usage


class WorkerSettings:
    functions = [
        run_integrations_post_workflow_run,
        upload_voicemail_audio_to_s3,
        process_workflow_completion,
        sync_campaign_source,
        process_campaign_batch,
        process_knowledge_base_document,
    ]
    cron_jobs = [
        # Hourly: flush per-key API request counters to the Sysevo api-usage-report fn.
        cron(flush_api_request_usage, minute=0),
        # Every 10 min: re-fire post-call wallet debits that never settled (crash / lost
        # job / transient webhook failure). Idempotent on workflow_run_id, so safe to retry.
        cron(reconcile_wallet_debits, minute=set(range(0, 60, 10))),
        # Every 10 min (offset by 5): re-fire post-call caller-memory extraction that never
        # settled. Idempotent per run (dedupe + upsert), so safe to retry. See H4.
        cron(reconcile_memory, minute=set(range(5, 60, 10))),
        # Hourly: attach Twilio's carrier charge to completed calls. Twilio
        # prices asynchronously, so this cannot be done at hang-up. Without it
        # every cost figure understates the truth by ~a third on real telephony.
        cron(reconcile_telephony_cost, minute={7}),
    ]
    redis_settings = REDIS_SETTINGS
    max_jobs = 10

    @staticmethod
    async def on_startup(_ctx) -> None:
        """Report at boot whether this worker can reach the api's temp directory.

        `process_workflow_completion` receives call audio and transcripts as
        FILESYSTEM PATHS written by the api container. That only works while both
        containers mount the same volume at /tmp. When a dedicated worker service
        was introduced on 2026-08-05 without `shared-tmp:/tmp`, every recording and
        transcript was silently discarded for six hours — the only symptom was one
        WARNING per call, and the UI simply showed an empty player.

        Sharing cannot be proven from one side, so this checks what it can and says
        what it sees. The hard alarm is the ARTIFACT LOST error in s3_upload.py;
        this is the early warning at boot.
        """
        import os
        import tempfile

        tmp = tempfile.gettempdir()
        try:
            probe = os.path.join(tmp, ".arq_worker_tmp_probe")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as e:
            logger.error(
                f"Worker temp dir {tmp} is not writable ({e}). Call recordings and "
                f"transcripts CANNOT be uploaded — check the shared-tmp volume mount."
            )
            return

        try:
            staged = [f for f in os.listdir(tmp) if f.endswith((".wav", ".txt"))]
            logger.info(
                f"ARQ worker temp dir {tmp}: writable, {len(staged)} staged artifact(s) visible. "
                f"If this stays at 0 while calls complete and ARTIFACT LOST appears, the api "
                f"and worker are NOT sharing /tmp and every recording is being discarded."
            )
        except Exception:
            pass


LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    # --- Handlers ---
    "handlers": {
        "console": {  # everything goes to stdout
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "level": "WARNING",  # only WARNING and above
            "formatter": "simple",
        },
    },
    # --- Formatters (optional) ---
    "formatters": {
        "simple": {
            "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        },
    },
    # --- Root logger ---
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    # --- Optionally silence Arq itself explicitly ---
    "loggers": {
        "arq": {  # arq.* loggers
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}


_redis_pool: ArqRedis | None = None


async def get_arq_redis() -> ArqRedis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = await create_pool(REDIS_SETTINGS)
    return _redis_pool


async def enqueue_job(function_name: FunctionNames, *args):
    redis = await get_arq_redis()
    await redis.enqueue_job(function_name, *args)
