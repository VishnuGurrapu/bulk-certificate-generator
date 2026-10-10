import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate
from app.schemas.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
)
from app.services.job_processor import MAX_RECIPIENTS_PER_JOB

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post(
    "",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create certificate generation job",
    description="Registers a new certificate generation job with its recipients in an atomic database transaction.",
)
def create_job(
    payload: JobCreateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> JobCreateResponse:
    """Create a certificate generation job and persist its recipients.

    Validates required fields, enforces recipient bounds, and guarantees atomic persistence.
    Does not trigger synchronous PDF generation or untracked background threads.
    """
    recipient_count = len(payload.recipients)
    if recipient_count > MAX_RECIPIENTS_PER_JOB:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Recipient count ({recipient_count}) exceeds the maximum limit of {MAX_RECIPIENTS_PER_JOB}.",
        )

    # Begin atomic transaction
    job = GenerationJob(
        event_name=payload.event_name,
        total_count=recipient_count,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.flush()  # Allocate job.id for foreign keys

    recipients = [
        RecipientCertificate(
            job_id=job.id,
            recipient_name=rec.name,
            recipient_email=str(rec.email),
        )
        for rec in payload.recipients
    ]
    db.add_all(recipients)
    db.commit()
    db.refresh(job)

    return JobCreateResponse.model_validate(job)


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get job status and progress",
    description="Retrieves the current status, counter metrics, and calculated progress percentage for a generation job.",
)
def get_job_status(
    job_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> JobStatusResponse:
    """Retrieve job details and progress information.

    Returns HTTP 404 if the job is not found.
    Handles zero-recipient jobs safely without division by zero.
    """
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    # Calculate progress percentage safely
    if job.total_count == 0:
        # A 0-recipient job has completed 100% of its workload if processed, or 0% otherwise
        progress = 100.0 if job.status == JobStatus.COMPLETED else 0.0
    else:
        progress = round((job.processed_count / job.total_count) * 100.0, 2)

    return JobStatusResponse(
        id=job.id,
        event_name=job.event_name,
        status=job.status,
        total_count=job.total_count,
        processed_count=job.processed_count,
        successful_count=job.successful_count,
        failed_count=job.failed_count,
        progress_percentage=progress,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )
