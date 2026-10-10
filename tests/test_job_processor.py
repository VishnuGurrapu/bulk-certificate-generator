import concurrent.futures
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import _get_connect_args, init_db
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate, RecipientStatus
from app.services.certificate_generator import (
    CertificateGenerationError,
    CertificateGenerationResult,
    CertificateGeneratorService,
)
from app.services.job_processor import (
    JobAlreadyCompletedError,
    JobAlreadyProcessingError,
    JobIncompleteProcessingError,
    JobNotFoundError,
    JobProcessorError,
    JobProcessorService,
    JobRecipientCountMismatchError,
    JobRecipientLimitExceededError,
    MAX_RECIPIENTS_PER_JOB,
    MissingRecipientError,
    process_generation_job,
)


@pytest.fixture
def test_engine(tmp_path: Path):
    """Create an isolated SQLite database file for testing."""
    test_db_path = tmp_path / "test_processor.db"
    db_url = f"sqlite:///{test_db_path.as_posix()}"
    engine = create_engine(
        db_url,
        connect_args=_get_connect_args(db_url),
    )
    init_db(target_engine=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(test_engine):
    """Return a sessionmaker bound to the test database engine."""
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine, class_=Session)


@pytest.fixture
def temp_storage_dir(tmp_path: Path) -> Path:
    """Provide a clean temporary directory for generated certificate PDFs."""
    storage_path = tmp_path / "test_certificates"
    storage_path.mkdir(parents=True, exist_ok=True)
    return storage_path


@pytest.fixture
def generator_service(temp_storage_dir: Path) -> CertificateGeneratorService:
    """Provide a real CertificateGeneratorService writing to temporary storage."""
    return CertificateGeneratorService(storage_dir=temp_storage_dir)


@pytest.fixture
def processor_service(session_factory, generator_service) -> JobProcessorService:
    """Provide a JobProcessorService configured with test session factory and generator."""
    return JobProcessorService(
        session_factory=session_factory,
        generator_service=generator_service,
    )


def test_process_job_all_success(processor_service, session_factory, temp_storage_dir) -> None:
    """Verify that a job with all valid recipients completes successfully."""
    # Create job and recipients
    with session_factory() as session:
        job = GenerationJob(
            event_name="Full Stack Python Summit",
            total_count=3,
        )
        session.add(job)
        session.commit()
        job_id = job.id

        recipients = [
            RecipientCertificate(
                job_id=job_id,
                recipient_name=f"Participant {i}",
                recipient_email=f"user{i}@example.com",
            )
            for i in range(1, 4)
        ]
        session.add_all(recipients)
        session.commit()

    # Process job
    final_job = processor_service.process_job(job_id)

    # Verify job state
    assert final_job.status == JobStatus.COMPLETED
    assert final_job.total_count == 3
    assert final_job.processed_count == 3
    assert final_job.successful_count == 3
    assert final_job.failed_count == 0
    assert final_job.started_at is not None
    assert final_job.completed_at is not None
    assert final_job.started_at.tzinfo == timezone.utc
    assert final_job.completed_at.tzinfo == timezone.utc

    # Verify each recipient in database and storage
    with session_factory() as session:
        reloaded_recipients = (
            session.query(RecipientCertificate)
            .filter_by(job_id=job_id)
            .order_by(RecipientCertificate.created_at.asc())
            .all()
        )
        assert len(reloaded_recipients) == 3
        for r in reloaded_recipients:
            assert r.status == RecipientStatus.COMPLETED
            assert r.certificate_code is not None
            assert r.certificate_code.startswith("CERT-")
            assert r.pdf_path is not None
            assert r.error_message is None
            assert r.processed_at is not None
            assert r.processed_at.tzinfo == timezone.utc

            # Check that PDF exists on disk
            pdf_file = temp_storage_dir / Path(r.pdf_path).name
            assert pdf_file.exists()
            assert pdf_file.stat().st_size > 0


def test_process_job_mixed_outcomes(processor_service, session_factory, generator_service) -> None:
    """Verify PARTIALLY_COMPLETED status when some recipients succeed and some fail."""
    with session_factory() as session:
        job = GenerationJob(event_name="AI Workshop", total_count=3)
        session.add(job)
        session.commit()
        job_id = job.id

        recipients = [
            RecipientCertificate(job_id=job_id, recipient_name="Alice Smith", recipient_email="alice@example.com"),
            RecipientCertificate(job_id=job_id, recipient_name="Fail User", recipient_email="fail@example.com"),
            RecipientCertificate(job_id=job_id, recipient_name="Bob Jones", recipient_email="bob@example.com"),
        ]
        session.add_all(recipients)
        session.commit()

    # Mock generator to fail specifically for "Fail User"
    real_generate = generator_service.generate_certificate

    def mock_generate(*args, **kwargs):
        if kwargs.get("recipient_name") == "Fail User":
            raise CertificateGenerationError("Simulated font rendering error")
        return real_generate(*args, **kwargs)

    generator_service.generate_certificate = mock_generate

    final_job = processor_service.process_job(job_id)

    # Verify job counts and status
    assert final_job.status == JobStatus.PARTIALLY_COMPLETED
    assert final_job.total_count == 3
    assert final_job.processed_count == 3
    assert final_job.successful_count == 2
    assert final_job.failed_count == 1
    assert final_job.completed_at is not None

    with session_factory() as session:
        recs = session.query(RecipientCertificate).filter_by(job_id=job_id).all()
        by_name = {r.recipient_name: r for r in recs}

        # Alice and Bob succeeded
        assert by_name["Alice Smith"].status == RecipientStatus.COMPLETED
        assert by_name["Alice Smith"].certificate_code is not None
        assert by_name["Bob Jones"].status == RecipientStatus.COMPLETED
        assert by_name["Bob Jones"].certificate_code is not None

        # Fail User failed with error message
        assert by_name["Fail User"].status == RecipientStatus.FAILED
        assert by_name["Fail User"].certificate_code is None
        assert by_name["Fail User"].pdf_path is None
        assert "Simulated font rendering error" in by_name["Fail User"].error_message
        assert by_name["Fail User"].processed_at is not None


def test_process_job_all_failures(processor_service, session_factory, generator_service) -> None:
    """Verify FAILED terminal status when every recipient in the batch fails."""
    with session_factory() as session:
        job = GenerationJob(event_name="Failing Workshop", total_count=2)
        session.add(job)
        session.commit()
        job_id = job.id

        recipients = [
            RecipientCertificate(job_id=job_id, recipient_name=f"User {i}", recipient_email=f"u{i}@example.com")
            for i in range(2)
        ]
        session.add_all(recipients)
        session.commit()

    # Mock generator to fail for all
    generator_service.generate_certificate = MagicMock(
        side_effect=CertificateGenerationError("Disk write quota exceeded")
    )

    final_job = processor_service.process_job(job_id)

    assert final_job.status == JobStatus.FAILED
    assert final_job.total_count == 2
    assert final_job.processed_count == 2
    assert final_job.successful_count == 0
    assert final_job.failed_count == 2
    assert final_job.completed_at is not None

    with session_factory() as session:
        recs = session.query(RecipientCertificate).filter_by(job_id=job_id).all()
        for r in recs:
            assert r.status == RecipientStatus.FAILED
            assert "Disk write quota exceeded" in r.error_message


def test_process_job_empty_job_completes_immediately(processor_service, session_factory) -> None:
    """Verify that an empty job (total_count=0, no recipients) completes immediately with zero counters."""
    with session_factory() as session:
        job = GenerationJob(event_name="Empty Event", total_count=0)
        session.add(job)
        session.commit()
        job_id = job.id

    final_job = processor_service.process_job(job_id)

    assert final_job.status == JobStatus.COMPLETED
    assert final_job.total_count == 0
    assert final_job.processed_count == 0
    assert final_job.successful_count == 0
    assert final_job.failed_count == 0
    assert final_job.started_at is not None
    assert final_job.completed_at is not None


def test_process_job_non_existent_id_raises_not_found(processor_service) -> None:
    """Verify that calling process_job with a non-existent UUID raises JobNotFoundError."""
    missing_id = uuid.uuid4()
    with pytest.raises(JobNotFoundError) as exc_info:
        processor_service.process_job(missing_id)
    assert exc_info.value.job_id == missing_id


def test_process_job_recipient_count_mismatch_fails_job(processor_service, session_factory) -> None:
    """Verify that a mismatch between total_count and actual persisted recipient rows fails the job."""
    with session_factory() as session:
        # Job declares total_count=3, but only has 1 recipient inserted
        job = GenerationJob(event_name="Mismatched Event", total_count=3)
        session.add(job)
        session.commit()
        job_id = job.id

        session.add(
            RecipientCertificate(job_id=job_id, recipient_name="Lone User", recipient_email="lone@example.com")
        )
        session.commit()

    with pytest.raises(JobRecipientCountMismatchError) as exc_info:
        processor_service.process_job(job_id)

    assert exc_info.value.total_count == 3
    assert exc_info.value.actual_count == 1

    with session_factory() as session:
        reloaded = session.get(GenerationJob, job_id)
        assert reloaded.status == JobStatus.FAILED
        assert reloaded.completed_at is not None


def test_process_job_exceeding_max_recipients_fails_job(processor_service, session_factory) -> None:
    """Verify that a job exceeding the 500-recipient limit fails and raises JobRecipientLimitExceededError."""
    with session_factory() as session:
        job = GenerationJob(event_name="Overloaded Event", total_count=501)
        session.add(job)
        session.commit()
        job_id = job.id

        recipients = [
            RecipientCertificate(job_id=job_id, recipient_name=f"User {i}", recipient_email=f"u{i}@test.com")
            for i in range(501)
        ]
        session.add_all(recipients)
        session.commit()

    with pytest.raises(JobRecipientLimitExceededError) as exc_info:
        processor_service.process_job(job_id)

    assert exc_info.value.recipient_count == 501
    assert exc_info.value.max_limit == MAX_RECIPIENTS_PER_JOB

    with session_factory() as session:
        reloaded = session.get(GenerationJob, job_id)
        assert reloaded.status == JobStatus.FAILED


def test_process_job_already_processing_raises_error(processor_service, session_factory) -> None:
    """Verify that attempting to process a job that is already PROCESSING raises JobAlreadyProcessingError."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Active Event",
            status=JobStatus.PROCESSING,
            total_count=1,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with pytest.raises(JobAlreadyProcessingError) as exc_info:
        processor_service.process_job(job_id)
    assert exc_info.value.job_id == job_id


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.COMPLETED, JobStatus.PARTIALLY_COMPLETED, JobStatus.FAILED],
)
def test_process_job_terminal_status_raises_already_completed(
    processor_service, session_factory, terminal_status
) -> None:
    """Verify that attempting to process a job in any terminal state raises JobAlreadyCompletedError."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Terminal Event",
            status=terminal_status,
            total_count=0,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with pytest.raises(JobAlreadyCompletedError) as exc_info:
        processor_service.process_job(job_id)
    assert exc_info.value.job_id == job_id
    assert exc_info.value.status == terminal_status


def test_atomic_job_claim_prevents_duplicate_concurrent_processing(
    session_factory, generator_service
) -> None:
    """Verify atomic claiming prevents two concurrent workers from both executing the same pending job."""
    with session_factory() as session:
        job = GenerationJob(event_name="Race Event", total_count=2)
        session.add(job)
        session.commit()
        job_id = job.id

        session.add_all([
            RecipientCertificate(job_id=job_id, recipient_name="User A", recipient_email="a@test.com"),
            RecipientCertificate(job_id=job_id, recipient_name="User B", recipient_email="b@test.com"),
        ])
        session.commit()

    service1 = JobProcessorService(session_factory=session_factory, generator_service=generator_service)
    service2 = JobProcessorService(session_factory=session_factory, generator_service=generator_service)

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker(svc):
        barrier.wait()
        try:
            res = svc.process_job(job_id)
            results.append(res)
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, service1)
        f2 = executor.submit(worker, service2)
        concurrent.futures.wait([f1, f2])

    # Exactly one worker should succeed, and exactly one should fail with JobAlreadyProcessingError or JobAlreadyCompletedError
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], (JobAlreadyProcessingError, JobAlreadyCompletedError))


def test_db_commit_failure_cleans_up_unreferenced_pdf(
    session_factory, generator_service, temp_storage_dir
) -> None:
    """Verify that if DB commit fails after PDF generation, unreferenced PDF is safely removed."""
    with session_factory() as session:
        job = GenerationJob(event_name="DB Error Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id, recipient_name="Crash User", recipient_email="crash@test.com"
        )
        session.add(recipient)
        session.commit()

    # Track generated file path
    generated_file_paths = []
    real_generate = generator_service.generate_certificate

    def spy_generate(*args, **kwargs):
        res = real_generate(*args, **kwargs)
        generated_file_paths.append(res.file_path)
        return res

    generator_service.generate_certificate = spy_generate

    # Create a custom session factory that fails on the specific recipient update commit
    class FailingSession(Session):
        def commit(self):
            # Inspect dirty objects to fail only recipient completion commit
            for obj in self.dirty:
                if isinstance(obj, RecipientCertificate) and obj.status == RecipientStatus.COMPLETED:
                    raise RuntimeError("Simulated SQLite disk/commit error")
            super().commit()

    failing_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=session_factory.kw["bind"], class_=FailingSession
    )

    processor = JobProcessorService(
        session_factory=failing_factory,
        generator_service=generator_service,
    )

    final_job = processor.process_job(job_id)

    # Job terminal status should reflect the recipient failure
    assert final_job.status == JobStatus.FAILED
    assert final_job.failed_count == 1
    assert final_job.successful_count == 0

    # The generated PDF must have been cleaned up and not left orphaned on disk
    assert len(generated_file_paths) == 1
    pdf_path = generated_file_paths[0]
    assert not pdf_path.exists()

    # Recipient status in DB reflects failure
    with session_factory() as session:
        rec = session.query(RecipientCertificate).filter_by(job_id=job_id).one()
        assert rec.status == RecipientStatus.FAILED
        assert "Database commit failed" in rec.error_message


def test_convenience_function_process_generation_job(session_factory, generator_service) -> None:
    """Verify the top-level process_generation_job helper function behaves identically."""
    with session_factory() as session:
        job = GenerationJob(event_name="Helper Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        session.add(
            RecipientCertificate(job_id=job_id, recipient_name="Helper User", recipient_email="help@test.com")
        )
        session.commit()

    final_job = process_generation_job(
        job_id=job_id,
        session_factory=session_factory,
        generator_service=generator_service,
    )

    assert final_job.status == JobStatus.COMPLETED
    assert final_job.successful_count == 1


def test_finalize_job_raises_when_counters_incomplete(processor_service, session_factory) -> None:
    """Verify that a job cannot be finalized if counters do not match total_count or sum."""
    with session_factory() as session:
        # Job has total_count=2, but processed_count=1
        job = GenerationJob(
            event_name="Incomplete Counters Event",
            total_count=2,
            processed_count=1,
            successful_count=1,
            failed_count=0,
            status=JobStatus.PROCESSING,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with pytest.raises(JobIncompleteProcessingError) as exc_info:
        processor_service._finalize_job_status(job_id)

    assert "processed_count (1) does not match total_count (2)" in str(exc_info.value)

    # Verify job was NOT marked with a terminal status
    with session_factory() as session:
        reloaded = session.get(GenerationJob, job_id)
        assert reloaded.status == JobStatus.PROCESSING


def test_finalize_job_raises_when_recipients_remain_unprocessed(
    processor_service, session_factory
) -> None:
    """Verify that a job cannot be finalized if any recipient remains PENDING or PROCESSING."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Unprocessed Recipient Event",
            total_count=1,
            processed_count=1,
            successful_count=1,
            failed_count=0,
            status=JobStatus.PROCESSING,
        )
        session.add(job)
        session.commit()
        job_id = job.id

        # Recipient is still in PENDING status
        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Pending Recipient",
            recipient_email="pending@test.com",
            status=RecipientStatus.PENDING,
        )
        session.add(recipient)
        session.commit()

    with pytest.raises(JobIncompleteProcessingError) as exc_info:
        processor_service._finalize_job_status(job_id)

    assert "recipient(s) remain incomplete" in str(exc_info.value)

    with session_factory() as session:
        reloaded = session.get(GenerationJob, job_id)
        assert reloaded.status == JobStatus.PROCESSING


def test_missing_recipient_raises_error_and_does_not_falsely_complete(
    processor_service, session_factory
) -> None:
    """Verify that if a recipient ID is missing when processing starts, MissingRecipientError is raised."""
    fake_recipient_id = uuid.uuid4()
    with pytest.raises(MissingRecipientError) as exc_info:
        processor_service._process_single_recipient(
            job_id=uuid.uuid4(),
            recipient_id=fake_recipient_id,
            event_name="Test Event",
            issue_date="October 11, 2026",
        )
    assert exc_info.value.recipient_id == fake_recipient_id


def test_persist_recipient_failure_db_error_propagates(session_factory, generator_service) -> None:
    """Verify that a database commit failure during failure recording propagates without being swallowed."""
    with session_factory() as session:
        job = GenerationJob(event_name="Failing DB Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Failure User",
            recipient_email="fail@test.com",
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    class CommitFailingSession(Session):
        def commit(self):
            # Fail whenever committing a FAILED recipient
            for obj in self.dirty:
                if isinstance(obj, RecipientCertificate) and obj.status == RecipientStatus.FAILED:
                    raise RuntimeError("Database connection broken")
            super().commit()

    failing_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=session_factory.kw["bind"], class_=CommitFailingSession
    )

    processor = JobProcessorService(
        session_factory=failing_factory,
        generator_service=generator_service,
    )

    with pytest.raises(JobProcessorError) as exc_info:
        processor._persist_recipient_failure(
            job_id=job_id,
            recipient_id=recipient_id,
            error_message="Test failure message",
        )

    assert "Failed to persist failure outcome" in str(exc_info.value)

    # Verify counters were NOT incremented
    with session_factory() as session:
        reloaded_job = session.get(GenerationJob, job_id)
        assert reloaded_job.processed_count == 0
        assert reloaded_job.failed_count == 0


def test_reconcile_preserves_file_when_db_verification_fails(
    session_factory, temp_storage_dir
) -> None:
    """Verify that if database state cannot be verified during reconciliation, the file is preserved."""
    test_pdf = temp_storage_dir / "preserve_test.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 mock content")
    assert test_pdf.exists()

    class VerifyFailingSession(Session):
        def get(self, *args, **kwargs):
            raise RuntimeError("Database offline during verification")

    failing_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=session_factory.kw["bind"], class_=VerifyFailingSession
    )

    processor = JobProcessorService(session_factory=failing_factory)

    with pytest.raises(JobProcessorError) as exc_info:
        processor._reconcile_and_cleanup_file_on_error(
            recipient_id=uuid.uuid4(),
            certificate_code="CERT-TEST-123",
            file_path=test_pdf,
            original_error=RuntimeError("Original commit failure"),
            job_id=uuid.uuid4(),
        )

    assert "Database state uncertain" in str(exc_info.value)
    # The file MUST be preserved
    assert test_pdf.exists()

