"""Reconcile the canonical and deployed dialer credential histories.

Both original migration chains are retained unchanged. The deployed singleton
wins; only an empty singleton can inherit the canonical table's encrypted row.
No credential is decrypted, logged, or overwritten.
"""
from alembic import op
import sqlalchemy as sa

revision = "vr20261026a"
down_revision = ("ef20261016", "e26a1015c001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO platform_twilio_dialer_credentials
            (id, account_sid, auth_token_encrypted, api_key_sid,
             api_key_secret_encrypted, twiml_app_sid, default_caller_id,
             last_validated_at, updated_at)
        SELECT 1, account_sid, auth_token_encrypted, api_key_sid,
               api_key_secret_encrypted, twiml_app_sid, default_caller_id,
               last_validated_at, updated_at
        FROM dialer_twilio_credentials
        WHERE id = 1
        ON CONFLICT (id) DO NOTHING
    """))


def downgrade() -> None:
    # Imported credentials remain intact during a migration rollback.
    pass
