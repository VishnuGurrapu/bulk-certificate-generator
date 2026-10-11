import enum
import uuid
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, Date, Enum, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.recipient import RecipientCertificate


class JobStatus(str, enum.Enum):
    """Lifecycle states of a bulk certificate generation job.

    - PENDING: Job has been registered and enqueued, but recipient processing has not started.
    - PROCESSING: Job has been picked up by a worker and certificates are actively being generated.
    - COMPLETED: Processing finished and every recipient certificate was generated successfully
      (successful_count == total_count).
    - PARTIALLY_COMPLETED: Processing finished, but at least one recipient failed and at least one
      succeeded (successful_count > 0 and failed_count > 0).
    - FAILED: Processing failed completely; either every recipient failed (failed_count == total_count)
      or an unhandled batch error aborted execution before any certificate could be created.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


class GenerationJob(Base):
    """Represents a bulk certificate generation job.

    Count semantics:
    - total_count: Total number of recipients in the requested batch.
    - processed_count: Number of recipients processed so far (processed_count = successful_count + failed_count).
    - successful_count: Number of recipients whose certificates were successfully rendered.
    - failed_count: Number of recipients whose certificate rendering encountered an error.
    """

    __tablename__ = "generation_jobs"

    __table_args__ = (
        CheckConstraint("total_count >= 0", name="chk_job_total_count_non_negative"),
        CheckConstraint("processed_count >= 0", name="chk_job_processed_count_non_negative"),
        CheckConstraint("successful_count >= 0", name="chk_job_successful_count_non_negative"),
        CheckConstraint("failed_count >= 0", name="chk_job_failed_count_non_negative"),
        CheckConstraint(
            "processed_count = successful_count + failed_count",
            name="chk_job_processed_sum",
        ),
        CheckConstraint("processed_count <= total_count", name="chk_job_processed_le_total"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    issue_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status_enum"),
        default=JobStatus.PENDING,
        nullable=False,
        index=True,
    )


    total_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    processed_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    successful_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    failed_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utc_now,
        nullable=False,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
    )

    # Cascade all deletes to recipients when a job is deleted
    recipients: Mapped[list["RecipientCertificate"]] = relationship(
        "RecipientCertificate",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"<GenerationJob(id={self.id}, event='{self.event_name}', "
            f"status='{self.status.value}', total={self.total_count}, "
            f"processed={self.processed_count})>"
        )
