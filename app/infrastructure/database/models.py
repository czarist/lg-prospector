"""Modelos SQLAlchemy — tabelas da plataforma."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base


def _uuid() -> str:
    return uuid4().hex


class CampaignStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ItemStatus(str, Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    NORMALIZED = "normalized"
    CONTACT_FOUND = "contact_found"
    EMAIL_FOUND = "email_found"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    CRM_CREATED = "crm_created"
    EMAIL_SENT = "email_sent"
    WAITING_RESPONSE = "waiting_response"
    RESPONDED = "responded"
    PIPELINE_UPDATED = "pipeline_updated"
    FAILED = "failed"
    SKIPPED = "skipped"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    niche: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=CampaignStatus.PENDING.value, index=True
    )
    # etapa atual: created|discover|enrich|crm|dispatch|done
    current_stage: Mapped[str] = mapped_column(String(32), default="created", index=True)
    max_results: Mapped[int] = mapped_column(Integer, default=20)
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[CampaignItem]] = relationship(
        "CampaignItem", back_populates="campaign", cascade="all, delete-orphan"
    )
    graph_runs: Mapped[list[GraphRun]] = relationship(
        "GraphRun", back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignItem(Base):
    __tablename__ = "campaign_items"
    __table_args__ = (Index("ix_campaign_items_campaign_status", "campaign_id", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default=ItemStatus.PENDING.value, index=True)
    # discovered|enriched|discarded|crm_synced|queued|sent|failed
    stage: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    company_domain: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    qualification_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    qualification_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crm_company_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crm_contact_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crm_lead_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    template_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    email_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    response_received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="items")
    company: Mapped[Optional[Company]] = relationship("Company", foreign_keys=[company_id])
    contact: Mapped[Optional[Contact]] = relationship("Contact", foreign_keys=[contact_id])


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        # Índice composto curto (utf8mb4: 191*3*4 = 2292 < 3072)
        UniqueConstraint("name", "city", "website_host", name="uq_company_name_city_host"),
        Index("ix_companies_segment", "segment"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(191), nullable=False, index=True)
    website: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    website_host: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    segment: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crm_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    extra: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contacts: Mapped[list[Contact]] = relationship("Contact", back_populates="company")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_email", "email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    company_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    linkedin: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crm_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    extra: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped[Optional[Company]] = relationship("Company", back_populates="contacts")
    emails: Mapped[list[EmailRecord]] = relationship("EmailRecord", back_populates="contact")


class EmailRecord(Base):
    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    contact_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    campaign_item_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("campaign_items.id", ondelete="SET NULL"), nullable=True
    )
    to_address: Mapped[str] = mapped_column(String(255), nullable=False)
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    body_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending, sent, failed, bounced
    message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    contact: Mapped[Optional[Contact]] = relationship("Contact", back_populates="emails")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (Index("ix_activities_campaign", "campaign_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    campaign_item_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("campaign_items.id", ondelete="SET NULL"), nullable=True
    )
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crm_activity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GraphRun(Base):
    __tablename__ = "graph_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_node: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="graph_runs")
    states: Mapped[list[GraphStateRecord]] = relationship(
        "GraphStateRecord", back_populates="graph_run", cascade="all, delete-orphan"
    )


class GraphStateRecord(Base):
    __tablename__ = "graph_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    graph_run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("graph_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    graph_run: Mapped[GraphRun] = relationship("GraphRun", back_populates="states")


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    niche: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    template_file: Mapped[str] = mapped_column(String(128), nullable=False)
    strategies: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
