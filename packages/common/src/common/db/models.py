"""SQLAlchemy models for every table in SPEC §5.

Enumerated columns are TEXT with CHECK constraints (easier to evolve than native PG enums).
Telemetry tables (`events`, `llm_calls`, `audit_log`, `external_api_usage`, `pipeline_runs`)
hold IDs without foreign keys so they survive user deletion/anonymization and stay cheap to write.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import TimestampedBase

# Fixed by the DB column type; must equal config `embeddings.dimensions` (checked at startup).
EMBEDDING_DIM = 1024


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    # NULL passes a CHECK, so this also works for nullable columns.
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"{column}_valid")


def _user_fk(nullable: bool = False) -> Any:
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable, index=True)


TextArray = ARRAY(Text)

USER_STATUSES = ("active", "paused", "deleted")
MAILBOX_PROVIDERS = ("gmail", "outlook")
MAILBOX_STATUSES = ("connected", "expired", "revoked")
MATCH_STATUSES = ("new", "shortlisted", "drafted", "skipped", "dismissed")
CONTACT_SOURCES = ("job_post", "careers_page", "hunter", "apollo", "name_pattern", "generic_inbox")
VERIFICATION_STATUSES = ("valid", "invalid", "catch_all", "unknown")
APPLICATION_STATUSES = (
    "drafting",
    "needs_edit",
    "awaiting_approval",
    "approved",
    "scheduled",
    "sent",
    "failed",
    "no_contact",
    "rejected_by_user",
    "replied",
    "bounced",
    "follow_up_sent",
)
SUPPRESSION_REASONS = ("opt_out", "bounce", "complaint", "manual")
EVENT_STATUSES = ("ok", "error", "skipped")
DATA_REQUEST_TYPES = ("export", "delete")
DATA_REQUEST_STATUSES = ("pending", "running", "completed", "failed")
APPROVAL_MODES = ("review_each", "auto_above_score")


class User(TimestampedBase):
    __tablename__ = "users"
    __table_args__ = (_one_of("status", USER_STATUSES),)

    cognito_sub: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), server_default="candidate")
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    timezone: Mapped[str | None] = mapped_column(String(64))


class Profile(TimestampedBase):
    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    phone: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    portfolio_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    total_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    current_title: Mapped[str | None] = mapped_column(Text)
    current_company: Mapped[str | None] = mapped_column(Text)
    notice_period: Mapped[str | None] = mapped_column(Text)
    work_authorization: Mapped[str | None] = mapped_column(Text)
    sponsorship_needed: Mapped[bool | None] = mapped_column(Boolean)
    pitch: Mapped[str | None] = mapped_column(Text)
    signature: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(String(32))
    profile_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class Resume(TimestampedBase):
    __tablename__ = "resumes"

    user_id: Mapped[uuid.UUID] = _user_fk()
    s3_key: Mapped[str] = mapped_column(Text)
    file_name: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confirmed_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="false")


class Preference(TimestampedBase):
    __tablename__ = "preferences"
    __table_args__ = (_one_of("approval_mode", APPROVAL_MODES),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    target_titles: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    seniority: Mapped[str | None] = mapped_column(String(32))
    must_have_keywords: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    excluded_keywords: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    job_types: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    work_modes: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    locations: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    open_to_relocate: Mapped[bool] = mapped_column(Boolean, server_default="false")
    min_salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    blocked_companies: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    blocked_industries: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    # Nullable: null means "use the config default".
    max_job_age_days: Mapped[int | None] = mapped_column(Integer)
    daily_cap: Mapped[int | None] = mapped_column(Integer)
    approval_mode: Mapped[str | None] = mapped_column(String(32))
    auto_send_min_score: Mapped[int | None] = mapped_column(Integer)
    follow_up_enabled: Mapped[bool | None] = mapped_column(Boolean)


class MailboxConnection(TimestampedBase):
    __tablename__ = "mailbox_connections"
    __table_args__ = (
        _one_of("provider", MAILBOX_PROVIDERS),
        _one_of("status", MAILBOX_STATUSES),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    provider: Mapped[str] = mapped_column(String(16))
    email: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    refresh_token_ciphertext: Mapped[bytes | None] = mapped_column()
    kms_key_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="connected")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Company(TimestampedBase):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text, index=True)
    ats: Mapped[str | None] = mapped_column(String(32))
    ats_slug: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)


class Job(TimestampedBase):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_jobs_posted_at", "posted_at"),
    )

    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    work_mode: Mapped[str | None] = mapped_column(String(16))
    employment_type: Mapped[str | None] = mapped_column(String(32))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    description: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_hash: Mapped[str] = mapped_column(String(64), unique=True)
    emails_in_post: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Match(TimestampedBase):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id"),
        _one_of("status", MATCH_STATUSES),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    vector_score: Mapped[float | None] = mapped_column(Float)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    fit_score: Mapped[int | None] = mapped_column(Integer)
    matched_skills: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    missing_skills: Mapped[list[str]] = mapped_column(TextArray, server_default="{}")
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="new")


class Contact(TimestampedBase):
    __tablename__ = "contacts"
    __table_args__ = (
        _one_of("source", CONTACT_SOURCES),
        _one_of("verification_status", VERIFICATION_STATUSES),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    verification_status: Mapped[str | None] = mapped_column(String(16))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Application(TimestampedBase):
    __tablename__ = "applications"
    __table_args__ = (
        _one_of("status", APPLICATION_STATUSES),
        Index("ix_applications_user_id_status", "user_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("matches.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), server_default="drafting")
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    claim_check_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    follow_up_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class SuppressionEntry(TimestampedBase):
    __tablename__ = "suppression_list"
    __table_args__ = (
        _one_of("reason", SUPPRESSION_REASONS),
        CheckConstraint("email IS NOT NULL OR domain IS NOT NULL", name="email_or_domain"),
    )

    email: Mapped[str | None] = mapped_column(Text, index=True)
    domain: Mapped[str | None] = mapped_column(Text, index=True)
    reason: Mapped[str] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class LlmCall(TimestampedBase):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    trace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    payload_s3_key: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    application_id: Mapped[uuid.UUID | None] = mapped_column()


class Event(TimestampedBase):
    """Append-only timeline. Written via `common.events.emit_event()`."""

    __tablename__ = "events"
    __table_args__ = (
        _one_of("status", EVENT_STATUSES),
        Index("ix_events_trace_id", "trace_id"),
        Index("ix_events_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_events_created_at", "created_at"),
    )

    trace_id: Mapped[str | None] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    stage: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class PipelineRun(TimestampedBase):
    __tablename__ = "pipeline_runs"

    trace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    new_jobs: Mapped[int] = mapped_column(Integer, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(16))


class ExternalApiUsage(TimestampedBase):
    __tablename__ = "external_api_usage"
    __table_args__ = (Index("ix_external_api_usage_created_at", "created_at"),)

    provider: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(Text)
    units: Mapped[Decimal] = mapped_column(Numeric(14, 4), server_default="1")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    status: Mapped[str] = mapped_column(String(16))


class AuditLog(TimestampedBase):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_at", "created_at"),)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)


class DataRequest(TimestampedBase):
    __tablename__ = "data_requests"
    __table_args__ = (
        _one_of("type", DATA_REQUEST_TYPES),
        _one_of("status", DATA_REQUEST_STATUSES),
    )

    user_id: Mapped[uuid.UUID] = _user_fk()
    type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
