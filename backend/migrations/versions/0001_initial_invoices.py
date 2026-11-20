"""Initial invoices table (matches the original create_all bootstrap).

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("original_data", sa.JSON(), nullable=False),
        sa.Column("corrected_data", sa.JSON(), nullable=False),
        sa.Column("validation_results", sa.JSON(), nullable=False),
        sa.Column("audit_history", sa.JSON(), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_invoices_sha256", "invoices", ["sha256"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invoices_sha256", table_name="invoices")
    op.drop_table("invoices")
