"""Organizations, users, accounting suggestions, DATEV export batches.

Existing invoices are moved into an ownerless "Default workspace"; the first account
registered afterwards becomes its admin.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""

from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("chart_of_accounts", sa.String(20), nullable=False, server_default="SKR03"),
        sa.Column("account_length", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("datev_consultant_number", sa.String(7)),
        sa.Column("datev_client_number", sa.String(5)),
        sa.Column("fiscal_year_start", sa.String(5), nullable=False, server_default="01-01"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])

    op.add_column("invoices", sa.Column("organization_id", sa.String(36)))
    op.add_column("invoices", sa.Column("extraction_method", sa.String(30), nullable=False, server_default="text"))
    op.add_column("invoices", sa.Column("transaction_facts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("invoices", sa.Column("fx_conversion", sa.JSON()))

    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM invoices")).scalar():
        legacy_id = str(uuid4())
        connection.execute(
            sa.text(
                "INSERT INTO organizations (id, name, chart_of_accounts, account_length, fiscal_year_start, created_at) "
                "VALUES (:id, 'Default workspace', 'SKR03', 4, '01-01', :now)"
            ),
            {"id": legacy_id, "now": datetime.now(timezone.utc)},
        )
        connection.execute(sa.text("UPDATE invoices SET organization_id = :id"), {"id": legacy_id})

    with op.batch_alter_table("invoices") as batch:
        batch.alter_column("organization_id", existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key("fk_invoices_organization", "organizations", ["organization_id"], ["id"], ondelete="CASCADE")
        batch.drop_index("ix_invoices_sha256")
        batch.create_index("ix_invoices_sha256", ["sha256"])
        batch.create_index("ix_invoices_organization_id", ["organization_id"])
        batch.create_unique_constraint("uq_invoice_org_sha256", ["organization_id", "sha256"])

    op.create_table(
        "account_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("expense_account", sa.String(9), nullable=False),
        sa.Column("creditor_account", sa.String(9), nullable=False),
        sa.Column("tax_key", sa.String(4)),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_account_mappings_organization_id", "account_mappings", ["organization_id"])
    op.create_table(
        "export_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("booking_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_export_batches_organization_id", "export_batches", ["organization_id"])
    op.create_table(
        "booking_suggestions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.String(36), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mapping_id", sa.String(36), sa.ForeignKey("account_mappings.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("booking", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("decided_by", sa.String(320)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("export_batch_id", sa.String(36), sa.ForeignKey("export_batches.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_booking_suggestions_organization_id", "booking_suggestions", ["organization_id"])
    op.create_index("ix_booking_suggestions_invoice_id", "booking_suggestions", ["invoice_id"], unique=True)


def downgrade() -> None:
    op.drop_table("booking_suggestions")
    op.drop_table("export_batches")
    op.drop_table("account_mappings")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("uq_invoice_org_sha256", type_="unique")
        batch.drop_index("ix_invoices_organization_id")
        batch.drop_index("ix_invoices_sha256")
        batch.create_index("ix_invoices_sha256", ["sha256"], unique=True)
        batch.drop_constraint("fk_invoices_organization", type_="foreignkey")
        batch.drop_column("fx_conversion")
        batch.drop_column("transaction_facts")
        batch.drop_column("extraction_method")
        batch.drop_column("organization_id")
    op.drop_table("memberships")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
