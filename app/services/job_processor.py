import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.job import GenerationJob, JobStatus, utc_now
from app.models.recipient import RecipientCertificate, RecipientStatus
from app.services.certificate_generator import (
    CertificateGenerationError,
    CertificateGeneratorService,
)

logger = logging.getLogger(__name__)

MAX_RECIPIENTS_PER_JOB = 500


class JobProcessorError(Exception):
    """Base exception for job processor service errors."""

    pass


class JobNotFoundError(JobProcessorError):
    """Raised when the specified job ID does not exist in the database."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"Job '{job_id}' not found")
        self.job_id = job_id


class JobAlreadyProcessingError(JobProcessorError):
    """Raised when attempting to process a job that is already actively being processed."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"Job '{job_id}' is already processing")
        self.job_id = job_id


class JobAlreadyCompletedError(JobProcessorError):
    """Raised when attempting to process a job that has already reached a terminal state."""

    def __init__(self, job_id: uuid.UUID, status: JobStatus) -> None:
        super().__init__(f"Job '{job_id}' is already in terminal state '{status.value}'")
        self.job_id = job_id
        self.status = status


class JobRecipientLimitExceededError(JobProcessorError):
    """Raised when a job exceeds the maximum permitted recipient limit."""

    def __init__(self, job_id: uuid.UUID, recipient_count: int, max_limit: int = MAX_RECIPIENTS_PER_JOB) -> None:
        super().__init__(
            f"Job '{job_id}' recipient count ({recipient_count}) exceeds limit of {max_limit}"
        )
        self.job_id = job_id
        self.recipient_count = recipient_count
        self.max_limit = max_limit


class JobRecipientCountMismatchError(JobProcessorError):
    """Raised when a job's persisted recipient count does not match total_count."""

    def __init__(self, job_id: uuid.UUID, total_count: int, actual_count: int) -> None:
        super().__init__(
            f"Job '{job_id}' total_count ({total_count}) does not match actual recipient count ({actual_count})"
        )
        self.job_id = job_id
        self.total_count = total_count
        self.actual_count = actual_count


class JobIncompleteProcessingError(JobProcessorError):
    """Raised when a job is attempted to be finalized before all recipients are fully processed."""

    def __init__(self, job_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"Cannot finalize job '{job_id}': {reason}")
        self.job_id = job_id
        self.reason = reason


class MissingRecipientError(JobProcessorError):
    """Raised when a recipient queued for processing cannot be found in the database."""

    def __init__(self, job_id: uuid.UUID, recipient_id: uuid.UUID) -> None:
        super().__init__(f"Recipient '{recipient_id}' not found for job '{job_id}'")
        self.job_id = job_id
        self.recipient_id = recipient_id


class JobProcessorService:
    """Background service responsible for executing bulk certificate generation jobs.

    Features:
    - Atomic job claiming via conditional SQL update (prevents race conditions between concurrent workers).
    - Validation of recipient constraints (max 500 recipients, total_count matching actual records).
    - Short-lived, isolated database sessions.
    - PDF rendering performed outside of database transactions.
    - Atomic per-recipient commit pairing recipient status with parent job counter increments.
    - Safe handling of database failures with orphan file reconciliation.
    - Strict server-side certificate code generation.
    """

    def __init__(
        self,
        session_factory: Optional[sessionmaker[Session]] = None,
        generator_service: Optional[CertificateGeneratorService] = None,
    ) -> None:
        """Initialize the processor with optional injected session factory and generator."""
        self.session_factory = session_factory or SessionLocal
        self.generator_service = generator_service or CertificateGeneratorService()

    def process_job(self, job_id: uuid.UUID) -> GenerationJob:
        """Process a persisted generation job and render certificates for all recipients.

        Args:
            job_id: The unique identifier of the generation job.

        Returns:
            The finalized GenerationJob entity state.

        Raises:
            JobNotFoundError: If the job does not exist.
            JobAlreadyProcessingError: If the job is already in PROCESSING status.
            JobAlreadyCompletedError: If the job is already in a terminal status.
            JobRecipientLimitExceededError: If the job exceeds 500 recipients.
            JobRecipientCountMismatchError: If total_count does not equal actual recipient rows.
        """
        # Step 1: Atomically claim job
        claim_time = utc_now()
        claimed = self._atomic_claim_job(job_id=job_id, started_at=claim_time)
        if not claimed:
            # Inspect existing state to raise specific service exception
            self._raise_unclaimed_job_error(job_id)

        # Step 2: Validate recipient batch consistency and limit
        event_name, issue_date, recipient_ids, total_count = self._load_and_validate_job(job_id)

        # Step 3: Handle genuinely empty job
        if total_count == 0:
            return self._finalize_empty_job(job_id, completed_at=claim_time)

        # Step 4: Process each recipient independently
        for recipient_id in recipient_ids:
            self._process_single_recipient(
                job_id=job_id,
                recipient_id=recipient_id,
                event_name=event_name,
                issue_date=issue_date,
            )

        # Step 5: Finalize job terminal status
        return self._finalize_job_status(job_id)

    def _atomic_claim_job(self, job_id: uuid.UUID, started_at: datetime) -> bool:
        """Attempt to atomically transition job from PENDING to PROCESSING.

        Returns True if claimed, False otherwise.
        """
        with self.session_factory() as session:
            stmt = (
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.PENDING,
                )
                .values(
                    status=JobStatus.PROCESSING,
                    started_at=started_at,
                )
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount > 0

    def _raise_unclaimed_job_error(self, job_id: uuid.UUID) -> None:
        """Inspect job in a fresh session and raise the appropriate domain error."""
        with self.session_factory() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status == JobStatus.PROCESSING:
                raise JobAlreadyProcessingError(job_id)
            if job.status in (JobStatus.COMPLETED, JobStatus.PARTIALLY_COMPLETED, JobStatus.FAILED):
                raise JobAlreadyCompletedError(job_id, job.status)
            raise JobProcessorError(f"Job '{job_id}' could not be claimed; unexpected status '{job.status.value}'")

    def _load_and_validate_job(
        self, job_id: uuid.UUID
    ) -> tuple[str, datetime, list[uuid.UUID], int]:
        """Load job details and recipient IDs, validating counts and limits.

        Fails job into FAILED terminal state if count constraints or limits are violated.
        """
        with self.session_factory() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise JobNotFoundError(job_id)

            total_count = job.total_count
            event_name = job.event_name
            # If issue_date column is not present on GenerationJob, default to created_at date
            issue_date = getattr(job, "issue_date", None) or job.created_at.date()

            # Query recipient IDs and count
            recipient_rows = (
                session.execute(
                    select(RecipientCertificate.id)
                    .where(RecipientCertificate.job_id == job_id)
                    .order_by(RecipientCertificate.created_at.asc(), RecipientCertificate.id.asc())
                )
                .scalars()
                .all()
            )
            actual_count = len(recipient_rows)

            # Check 500-recipient limit
            if actual_count > MAX_RECIPIENTS_PER_JOB:
                job.status = JobStatus.FAILED
                job.completed_at = utc_now()
                session.commit()
                raise JobRecipientLimitExceededError(job_id, actual_count, MAX_RECIPIENTS_PER_JOB)

            # Check total_count matches actual recipient count
            if total_count != actual_count:
                job.status = JobStatus.FAILED
                job.completed_at = utc_now()
                session.commit()
                raise JobRecipientCountMismatchError(job_id, total_count, actual_count)

            return event_name, issue_date, list(recipient_rows), total_count

    def _finalize_empty_job(self, job_id: uuid.UUID, completed_at: datetime) -> GenerationJob:
        """Complete a genuinely empty job (total_count == 0)."""
        with self.session_factory() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            job.status = JobStatus.COMPLETED
            job.completed_at = completed_at
            session.commit()
            session.refresh(job)
            return job

    def _process_single_recipient(
        self,
        job_id: uuid.UUID,
        recipient_id: uuid.UUID,
        event_name: str,
        issue_date: Union[datetime, str],
    ) -> None:
        """Process an individual recipient: render PDF and persist outcome with job counter update.

        Raises MissingRecipientError if the recipient is missing.
        Propagates any database persistence failures.
        """
        # 1. Fetch recipient data and mark PROCESSING in a brief transaction
        recipient_name = self._mark_recipient_processing(job_id=job_id, recipient_id=recipient_id)

        # 2. Render PDF (outside of any open database transaction)
        generation_error: Optional[Exception] = None
        generation_result = None
        try:
            # Strictly server-generated certificate code (custom_code=None)
            generation_result = self.generator_service.generate_certificate(
                recipient_name=recipient_name,
                event_name=event_name,
                issue_date=issue_date,
                custom_code=None,
            )
        except Exception as e:
            generation_error = e
            logger.warning(
                "PDF generation failed for recipient '%s' in job '%s': %s",
                recipient_id,
                job_id,
                e,
            )

        # 3. Persist outcome atomically with job counter update
        if generation_result is not None:
            self._persist_recipient_success(
                job_id=job_id,
                recipient_id=recipient_id,
                certificate_code=generation_result.certificate_code,
                file_path=generation_result.file_path,
            )
        else:
            error_msg = str(generation_error) if generation_error else "Unknown generation error"
            self._persist_recipient_failure(
                job_id=job_id,
                recipient_id=recipient_id,
                error_message=error_msg[:1000],
            )

    def _mark_recipient_processing(self, job_id: uuid.UUID, recipient_id: uuid.UUID) -> str:
        """Mark recipient as PROCESSING and return the recipient's name.

        Raises MissingRecipientError if recipient cannot be found.
        """
        with self.session_factory() as session:
            recipient = session.get(RecipientCertificate, recipient_id)
            if recipient is None:
                raise MissingRecipientError(job_id=job_id, recipient_id=recipient_id)
            recipient.status = RecipientStatus.PROCESSING
            session.commit()
            return recipient.recipient_name

    def _persist_recipient_success(
        self,
        job_id: uuid.UUID,
        recipient_id: uuid.UUID,
        certificate_code: str,
        file_path: Path,
    ) -> None:
        """Persist successful generation and increment job counters atomically.

        Reconciles orphaned files if the database commit fails.
        """
        storage_settings = get_settings()
        base_storage = Path(storage_settings.storage_dir).resolve()
        try:
            rel_path = file_path.relative_to(base_storage)
            pdf_path_str = str(rel_path).replace("\\", "/")
        except ValueError:
            pdf_path_str = str(file_path).replace("\\", "/")

        processed_time = utc_now()

        try:
            with self.session_factory() as session:
                recipient = session.get(RecipientCertificate, recipient_id)
                job = session.get(GenerationJob, job_id)
                if recipient is None or job is None:
                    raise JobProcessorError("Recipient or Job record missing during success persist")

                recipient.status = RecipientStatus.COMPLETED
                recipient.certificate_code = certificate_code
                recipient.pdf_path = pdf_path_str
                recipient.error_message = None
                recipient.processed_at = processed_time

                job.processed_count += 1
                job.successful_count += 1

                session.commit()
        except Exception as db_err:
            logger.error(
                "Database error committing success for recipient '%s' in job '%s': %s",
                recipient_id,
                job_id,
                db_err,
            )
            # Reconcile: check in a fresh session whether the record actually committed
            self._reconcile_and_cleanup_file_on_error(
                recipient_id=recipient_id,
                certificate_code=certificate_code,
                file_path=file_path,
                original_error=db_err,
                job_id=job_id,
            )

    def _reconcile_and_cleanup_file_on_error(
        self,
        recipient_id: uuid.UUID,
        certificate_code: str,
        file_path: Path,
        original_error: Exception,
        job_id: uuid.UUID,
    ) -> None:
        """Reconcile file cleanup on database commit failure.

        If the database record did NOT persist the file reference, safely delete the file
        and attempt to record the failure. If recording failure fails or state cannot be verified,
        propagate the error so processing stops immediately.
        """
        persisted_referencing_record = False
        try:
            with self.session_factory() as verify_session:
                rec = verify_session.get(RecipientCertificate, recipient_id)
                if rec and rec.certificate_code == certificate_code and rec.status == RecipientStatus.COMPLETED:
                    persisted_referencing_record = True
        except Exception as verify_err:
            # Cannot verify DB state with certainty; preserve file to avoid deleting valid certificate
            logger.error(
                "Could not verify DB state for recipient '%s' (error: %s). Preserving file '%s'",
                recipient_id,
                verify_err,
                file_path,
            )
            raise JobProcessorError(
                f"Database state uncertain for recipient '{recipient_id}' after error '{original_error}'. Preserving file."
            ) from verify_err

        if not persisted_referencing_record:
            # Safe to establish that no persisted record references this file
            try:
                if file_path.exists():
                    file_path.unlink(missing_ok=True)
                    logger.info("Cleaned up unreferenced PDF file '%s' after DB failure", file_path)
            except OSError as cleanup_err:
                logger.warning("Failed to remove unreferenced PDF file '%s': %s", file_path, cleanup_err)

            # Record failure in database so counters remain consistent; propagate if this fails
            self._persist_recipient_failure(
                job_id=job_id,
                recipient_id=recipient_id,
                error_message=f"Database commit failed: {original_error}"[:1000],
            )
        else:
            # The record was already persisted (e.g. commit succeeded but post-commit or socket errored)
            logger.info("Recipient '%s' was already successfully committed in DB. Preserving file '%s'", recipient_id, file_path)

    def _persist_recipient_failure(
        self,
        job_id: uuid.UUID,
        recipient_id: uuid.UUID,
        error_message: str,
    ) -> None:
        """Persist failed generation and increment job failed_count atomically.

        Propagates any database failure so errors are never silently swallowed.
        """
        processed_time = utc_now()
        try:
            with self.session_factory() as session:
                recipient = session.get(RecipientCertificate, recipient_id)
                job = session.get(GenerationJob, job_id)
                if recipient is None or job is None:
                    raise JobProcessorError(f"Recipient '{recipient_id}' or Job '{job_id}' missing during failure persist")

                recipient.status = RecipientStatus.FAILED
                recipient.error_message = error_message
                recipient.processed_at = processed_time

                job.processed_count += 1
                job.failed_count += 1

                session.commit()
        except Exception as db_err:
            logger.critical(
                "CRITICAL: Failed to persist failure status for recipient '%s' in job '%s': %s",
                recipient_id,
                job_id,
                db_err,
            )
            if isinstance(db_err, JobProcessorError):
                raise
            raise JobProcessorError(
                f"Failed to persist failure outcome for recipient '{recipient_id}': {db_err}"
            ) from db_err

    def _finalize_job_status(self, job_id: uuid.UUID) -> GenerationJob:
        """Verify processing completeness and persist terminal status for the job.

        Raises JobIncompleteProcessingError if counters or recipient statuses are incomplete.
        """
        with self.session_factory() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise JobNotFoundError(job_id)

            # 1. Verify processed_count == total_count
            if job.processed_count != job.total_count:
                raise JobIncompleteProcessingError(
                    job_id=job_id,
                    reason=f"processed_count ({job.processed_count}) does not match total_count ({job.total_count})",
                )

            # 2. Verify processed_count == successful_count + failed_count
            if job.processed_count != (job.successful_count + job.failed_count):
                raise JobIncompleteProcessingError(
                    job_id=job_id,
                    reason=(
                        f"processed_count ({job.processed_count}) != "
                        f"successful_count ({job.successful_count}) + failed_count ({job.failed_count})"
                    ),
                )

            # 3. Verify no recipient remains in PENDING or PROCESSING
            unprocessed_recipients = (
                session.execute(
                    select(RecipientCertificate.id, RecipientCertificate.status)
                    .where(
                        RecipientCertificate.job_id == job_id,
                        RecipientCertificate.status.in_([RecipientStatus.PENDING, RecipientStatus.PROCESSING]),
                    )
                )
                .all()
            )
            if unprocessed_recipients:
                unprocessed_summary = ", ".join(f"{r.id}:{r.status.value}" for r in unprocessed_recipients[:5])
                raise JobIncompleteProcessingError(
                    job_id=job_id,
                    reason=f"{len(unprocessed_recipients)} recipient(s) remain incomplete: [{unprocessed_summary}]",
                )

            # Compute terminal status
            if job.failed_count == job.total_count:
                job.status = JobStatus.FAILED
            elif job.failed_count > 0:
                job.status = JobStatus.PARTIALLY_COMPLETED
            else:
                job.status = JobStatus.COMPLETED

            job.completed_at = utc_now()
            session.commit()
            session.refresh(job)
            return job


def process_generation_job(
    job_id: uuid.UUID,
    session_factory: Optional[sessionmaker[Session]] = None,
    generator_service: Optional[CertificateGeneratorService] = None,
) -> GenerationJob:
    """Convenience helper function to execute a bulk generation job."""
    service = JobProcessorService(
        session_factory=session_factory,
        generator_service=generator_service,
    )
    return service.process_job(job_id)
