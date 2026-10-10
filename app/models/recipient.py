import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Enum, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.job import GenerationJob


class RecipientStatus(str, enum.Enum):
    """Lifecycle states of an individual recipient's certificate generation.

    - PENDING: Queued, awaiting certificate rendering.
    - PROCESSING: Currently being rendered into a PDF.
    - COMPLETED: PDF successfully rendered and stored.
    - FAILED: Encountered an error during processing; error details captured in error_message.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


class RecipientCertificate(Base):
    """Represents an individual recipient and their generated certificate record."""

    __tablename__ = "recipient_certificates"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    recipient_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    status: Mapped[RecipientStatus] = mapped_column(
        Enum(RecipientStatus, name="recipient_status_enum"),
        default=RecipientStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Unique identifier for the certificate (e.g. CERT-xxxx or UUID string)
    certificate_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        unique=True,
        nullable=True,
        index=True,
    )

    # Nullable relative file path within the application storage directory
    pdf_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )

    # Safely limited error message explaining individual rendering failure
    error_message: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utc_now,
        nullable=False,
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
    )

    # Back-reference to the parent job
    job: Mapped["GenerationJob"] = relationship(
        "GenerationJob",
        back_populates="recipients",
    )

    def __repr__(self) -> str:
        return (
            f"<RecipientCertificate(id={self.id}, job_id={self.job_id}, "
            f"name='{self.recipient_name}', status='{self.status.value}')>"
        )
