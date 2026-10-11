import logging
import uuid
from pathlib import Path
from typing import Annotated, Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate, RecipientStatus
from app.schemas.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobRecipientResponse,
    JobStatusResponse,
)
from app.services.certificate_generator import CertificateGeneratorService
from app.services.job_processor import (
    MAX_RECIPIENTS_PER_JOB,
    JobAlreadyCompletedError,
    JobAlreadyProcessingError,
    JobProcessorService,
)


router = APIRouter(prefix="/jobs", tags=["Jobs"])


def _run_job_processor_background(
    job_id: uuid.UUID,
    bind_engine: Any,
    storage_dir: str,
) -> None:
    """Execute automatic certificate generation for a job in the background."""
    logger.info("Starting automatic background processing for job '%s'", job_id)
    try:
        session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=bind_engine,
            class_=Session,
        )
        generator_service = CertificateGeneratorService(storage_dir=storage_dir)
        processor = JobProcessorService(
            session_factory=session_factory,
            generator_service=generator_service,
        )
        processor.process_job(job_id)
        logger.info("Automatic background processing completed successfully for job '%s'", job_id)
    except (JobAlreadyProcessingError, JobAlreadyCompletedError) as exc:
        logger.warning(
            "Background processing skipped for job '%s' (already claimed or terminal): %s",
            job_id,
            exc,
        )
    except Exception as exc:
        logger.exception(
            "Unhandled exception during automatic background processing for job '%s': %s",
            job_id,
            exc,
        )


@router.post(
    "",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create certificate generation job",
    description="Registers a new certificate generation job with its recipients in an atomic database transaction and triggers automatic background processing.",
)
def create_job(
    payload: JobCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobCreateResponse:
    """Create a certificate generation job and persist its recipients.

    Validates required fields, enforces recipient bounds, and guarantees atomic persistence.
    Triggers automatic in-process background processing after the transaction is committed.
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
        issue_date=payload.issue_date,
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

    # Trigger automatic background processing only after database commit succeeds
    background_tasks.add_task(
        _run_job_processor_background,
        job_id=job.id,
        bind_engine=db.get_bind(),
        storage_dir=settings.storage_dir,
    )

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

    recipients_list = [
        JobRecipientResponse(
            recipient_id=r.id,
            name=r.recipient_name,
            email=r.recipient_email,
            status=r.status,
        )
        for r in job.recipients
    ]

    return JobStatusResponse(
        id=job.id,
        event_name=job.event_name,
        issue_date=job.issue_date,
        status=job.status,
        total_count=job.total_count,
        processed_count=job.processed_count,
        successful_count=job.successful_count,
        failed_count=job.failed_count,
        progress_percentage=progress,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        recipients=recipients_list,
    )


@router.get(
    "/{job_id}/recipients/{recipient_id}/certificate",
    response_class=FileResponse,
    status_code=status.HTTP_200_OK,
    summary="Download recipient certificate PDF",
    description="Serves the generated PDF certificate for a recipient belonging to the specified generation job.",
)
def get_recipient_certificate(
    job_id: uuid.UUID,
    recipient_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """Retrieve and download a successfully generated certificate PDF.

    Validates:
    - Job exists (404).
    - Recipient exists (404).
    - Recipient belongs to the requested job (404).
    - Certificate generation status is COMPLETED (400 if pending/processing/failed).
    - Stored path is secure, within storage directory, and exists on disk (404 if missing or invalid).
    """
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    recipient = db.get(RecipientCertificate, recipient_id)
    if recipient is None or recipient.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recipient with ID '{recipient_id}' not found for job '{job_id}'.",
        )

    if recipient.status != RecipientStatus.COMPLETED or not recipient.pdf_path:
        # Sanitize error detail: do not expose internal exception stack traces or DB errors to API clients
        if recipient.status == RecipientStatus.FAILED and recipient.error_message:
            logger.warning(
                "Certificate download requested for failed recipient '%s' (job '%s'). Diagnostics: %s",
                recipient_id,
                job_id,
                recipient.error_message,
            )

        error_detail = (
            f"Certificate for recipient '{recipient_id}' is not available. Status: '{recipient.status.value}'."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail,
        )

    # Validate file path securely
    base_storage = Path(settings.storage_dir).resolve()
    stored_path = Path(recipient.pdf_path)



    if stored_path.is_absolute():
        target_path = stored_path.resolve()
    else:
        target_path = (base_storage / stored_path).resolve()

    # Path traversal validation
    try:
        if not target_path.is_relative_to(base_storage):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Certificate file path is invalid.",
            )
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Certificate file path is invalid.",
        )

    if not target_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Certificate file not found on storage.",
        )

    # Construct clean download filename
    code_part = recipient.certificate_code or str(recipient.id)
    download_filename = f"{code_part}.pdf"

    return FileResponse(
        path=target_path,
        media_type="application/pdf",
        filename=download_filename,
    )
