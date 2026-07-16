"""strip provider secrets from workflow_configurations

Removes plaintext provider secrets (api_key / credentials / aws_*) that were
previously persisted into ``workflow_configurations.model_overrides`` on every
agent save. Provider keys are re-injected at runtime from the org's (now
encrypted) provider connections and the user's global config — see
run_pipeline's enrich_* calls — so deleting the persisted copies does not affect
provider auth on calls.

Idempotent (only rewrites rows that still contain a secret). Irreversible: the
secrets are deleted, not relocated, so downgrade is a no-op.

Revision ID: e2f3a4b5c6d7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-16
"""
import json

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e2f3a4b5c6d7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_SECRET_FIELDS = ("api_key", "credentials", "aws_access_key", "aws_secret_key")
_SECTIONS = ("llm", "tts", "stt", "realtime")


def _strip(cfg):
    """Return (config, changed) with secret fields removed from model_overrides."""
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            return cfg, False
    if not isinstance(cfg, dict):
        return cfg, False
    overrides = cfg.get("model_overrides")
    if not isinstance(overrides, dict):
        return cfg, False
    changed = False
    for section in _SECTIONS:
        section_cfg = overrides.get(section)
        if isinstance(section_cfg, dict):
            for field in _SECRET_FIELDS:
                if field in section_cfg:
                    del section_cfg[field]
                    changed = True
    return cfg, changed


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("workflows", "workflow_definitions"):
        rows = conn.execute(
            sa.text(
                f"SELECT id, workflow_configurations FROM {table} "
                "WHERE workflow_configurations IS NOT NULL"
            )
        ).fetchall()
        for rid, cfg in rows:
            new_cfg, changed = _strip(cfg)
            if changed:
                conn.execute(
                    sa.text(
                        f"UPDATE {table} SET workflow_configurations = CAST(:c AS json) "
                        "WHERE id = :id"
                    ),
                    {"c": json.dumps(new_cfg), "id": rid},
                )


def downgrade() -> None:
    # Irreversible: secrets were deleted (they are re-resolved at runtime). No-op.
    pass
