"""add per-agent knowledge base assignments

Revision ID: e1f2a3b4c5d6
Revises: f4dd932f85d0
Create Date: 2026-05-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'f4dd932f85d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_global flag to knowledge_base_documents
    op.add_column(
        "knowledge_base_documents",
        sa.Column("is_global", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "ix_kb_documents_is_global",
        "knowledge_base_documents",
        ["organization_id", "is_global"],
        unique=False,
    )

    # Create workflow_document_assignments table
    op.create_table(
        "workflow_document_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_base_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "document_id", name="uq_workflow_document_assignment"),
    )
    op.create_index(op.f("ix_wda_workflow_id"), "workflow_document_assignments", ["workflow_id"], unique=False)
    op.create_index(op.f("ix_wda_document_id"), "workflow_document_assignments", ["document_id"], unique=False)
    op.create_index(op.f("ix_wda_organization_id"), "workflow_document_assignments", ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wda_organization_id"), table_name="workflow_document_assignments")
    op.drop_index(op.f("ix_wda_document_id"), table_name="workflow_document_assignments")
    op.drop_index(op.f("ix_wda_workflow_id"), table_name="workflow_document_assignments")
    op.drop_table("workflow_document_assignments")
    op.drop_index("ix_kb_documents_is_global", table_name="knowledge_base_documents")
    op.drop_column("knowledge_base_documents", "is_global")
