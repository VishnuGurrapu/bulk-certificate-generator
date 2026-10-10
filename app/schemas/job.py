import re
import uuid
from datetime import date, datetime
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.job import JobStatus
from app.services.job_processor import MAX_RECIPIENTS_PER_JOB

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecipientCreate(BaseModel):
    """Schema for an individual recipient in a job creation request."""

    name: Annotated[str, Field(min_length=1, max_length=255, description="Full name of recipient")]
    email: Annotated[str, Field(min_length=3, max_length=255, description="Valid email address of recipient")]

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        """Validate email format using standard email pattern."""
        if not EMAIL_REGEX.match(v):
            raise ValueError(f"Invalid email address format: '{v}'")
        return v.lower()



class JobCreateRequest(BaseModel):
    """Request schema for creating a certificate generation job."""

    event_name: Annotated[str, Field(min_length=1, max_length=255, description="Name of the event or course")]
    issue_date: Annotated[Optional[date], Field(default=None, description="Issue date on certificates (defaults to current date)")]
    recipients: Annotated[
        list[RecipientCreate],
        Field(
            default_factory=list,
            max_length=MAX_RECIPIENTS_PER_JOB,
            description=f"List of recipients (up to {MAX_RECIPIENTS_PER_JOB})",
        ),
    ]

    model_config = ConfigDict(str_strip_whitespace=True)


class JobCreateResponse(BaseModel):
    """Response schema returned immediately after job creation."""

    id: uuid.UUID
    event_name: str
    status: JobStatus
    total_count: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobStatusResponse(BaseModel):
    """Response schema for querying job status and progress."""

    id: uuid.UUID
    event_name: str
    status: JobStatus
    total_count: int
    processed_count: int
    successful_count: int
    failed_count: int
    progress_percentage: float = Field(
        description="Percentage of recipients processed, from 0.0 to 100.0"
    )
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
