"""initial_schema

Revision ID: c07ceb9f31b0
Revises:
Create Date: 2026-08-04 15:07:41.040342

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c07ceb9f31b0"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("niche", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("query", sa.String(length=512), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("max_results", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_niche", "campaigns", ["niche"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "companies",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=191), nullable=False),
        sa.Column("website", sa.String(length=512), nullable=True),
        sa.Column("website_host", sa.String(length=191), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("segment", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("crm_id", sa.String(length=64), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "city", "website_host", name="uq_company_name_city_host"),
    )
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_segment", "companies", ["segment"])
    op.create_index("ix_companies_crm_id", "companies", ["crm_id"])

    op.create_table(
        "providers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("niche", sa.String(length=64), nullable=False),
        sa.Column("template_file", sa.String(length=128), nullable=False),
        sa.Column("strategies", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_providers_niche", "providers", ["niche"])

    op.create_table(
        "contacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("company_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=128), nullable=True),
        sa.Column("linkedin", sa.String(length=512), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("crm_id", sa.String(length=64), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contacts_email", "contacts", ["email"])
    op.create_index("ix_contacts_crm_id", "contacts", ["crm_id"])

    op.create_table(
        "campaign_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32), nullable=False),
        sa.Column("company_id", sa.String(length=32), nullable=True),
        sa.Column("contact_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("qualification_score", sa.Integer(), nullable=True),
        sa.Column("qualification_notes", sa.Text(), nullable=True),
        sa.Column("crm_company_id", sa.String(length=64), nullable=True),
        sa.Column("crm_contact_id", sa.String(length=64), nullable=True),
        sa.Column("crm_lead_id", sa.String(length=64), nullable=True),
        sa.Column("template_name", sa.String(length=128), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_items_status", "campaign_items", ["status"])
    op.create_index("ix_campaign_items_campaign_status", "campaign_items", ["campaign_id", "status"])

    op.create_table(
        "emails",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("contact_id", sa.String(length=32), nullable=True),
        sa.Column("campaign_item_id", sa.String(length=32), nullable=True),
        sa.Column("to_address", sa.String(length=255), nullable=False),
        sa.Column("from_address", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("template_name", sa.String(length=128), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_item_id"], ["campaign_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "activities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("campaign_item_id", sa.String(length=32), nullable=True),
        sa.Column("activity_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("crm_activity_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_item_id"], ["campaign_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activities_campaign", "activities", ["campaign_id"])

    op.create_table(
        "graph_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_node", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id"),
    )
    op.create_index("ix_graph_runs_campaign_id", "graph_runs", ["campaign_id"])
    op.create_index("ix_graph_runs_status", "graph_runs", ["status"])

    op.create_table(
        "graph_state",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("graph_run_id", sa.String(length=32), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column("state_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["graph_run_id"], ["graph_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_graph_state_graph_run_id", "graph_state", ["graph_run_id"])


def downgrade() -> None:
    op.drop_table("graph_state")
    op.drop_table("graph_runs")
    op.drop_table("activities")
    op.drop_table("emails")
    op.drop_table("campaign_items")
    op.drop_table("contacts")
    op.drop_table("providers")
    op.drop_table("companies")
    op.drop_table("campaigns")
