"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-01-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clinics",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_name", sa.String(), nullable=True),
        sa.Column("phone_number", sa.String(), nullable=True),
        sa.Column("whatsapp_phone_number_id", sa.String(), nullable=True),
        sa.Column("clinic_type", sa.String(), nullable=True),
        sa.Column("services", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("slot_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_clinics_whatsapp_phone_number_id", "clinics", ["whatsapp_phone_number_id"], unique=False)

    op.create_table(
        "clinic_whatsapp_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("clinic_id", sa.String(), sa.ForeignKey("clinics.id"), nullable=False, unique=True),
        sa.Column("phone_number_id", sa.String(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("waba_id", sa.String(), nullable=True),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("unique_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.create_index("ix_users_unique_id", "users", ["unique_id"], unique=False)

    op.create_table(
        "user_clinic",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("clinic_id", sa.String(), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("visit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("crm_info", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_draft", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("user_id", "clinic_id", name="uq_user_clinic"),
    )

    op.create_table(
        "appointments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clinic_id", sa.String(), sa.ForeignKey("clinics.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("age", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("date", sa.String(), nullable=True),
        sa.Column("time", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="confirmed"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("appointment_at", sa.DateTime(), nullable=True),
        sa.Column("is_walkin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_appointments_clinic_id", "appointments", ["clinic_id"], unique=False)
    op.create_index("ix_appointments_user_id", "appointments", ["user_id"], unique=False)
    op.create_index("ix_appointments_phone", "appointments", ["phone"], unique=False)

    op.create_table(
        "appointment_checkins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("appointments.id"), nullable=False, unique=True),
        sa.Column("token_code", sa.String(), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("checked_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_in_at", sa.DateTime(), nullable=True),
        sa.Column("token", sa.String(), nullable=True),
    )

    op.create_table(
        "appointment_checkouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("appointments.id"), nullable=False, unique=True),
        sa.Column("token_code", sa.String(), nullable=True),
        sa.Column("checked_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_out_at", sa.DateTime(), nullable=True),
        sa.Column("qr_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("appointment_checkouts")
    op.drop_table("appointment_checkins")
    op.drop_index("ix_appointments_phone", table_name="appointments")
    op.drop_index("ix_appointments_user_id", table_name="appointments")
    op.drop_index("ix_appointments_clinic_id", table_name="appointments")
    op.drop_table("appointments")
    op.drop_table("user_clinic")
    op.drop_index("ix_users_unique_id", table_name="users")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_table("users")
    op.drop_table("clinic_whatsapp_credentials")
    op.drop_index("ix_clinics_whatsapp_phone_number_id", table_name="clinics")
    op.drop_table("clinics")
