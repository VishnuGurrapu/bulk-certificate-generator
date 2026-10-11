import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pypdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import _get_connect_args, get_db, init_db
from app.main import app
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate, RecipientStatus
from app.services.certificate_generator import CertificateGeneratorService
from app.services.job_processor import (
    MAX_RECIPIENTS_PER_JOB,
    JobAlreadyCompletedError,
    JobProcessorService,
)


@pytest.fixture
def test_engine(tmp_path: Path):
    """Create an isolated SQLite database file for testing."""
    test_db_path = tmp_path / "test_api_jobs.db"
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
    """Provide a temporary directory for certificate PDF storage in API tests."""
    storage = tmp_path / "storage" / "certificates"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@pytest.fixture
def client(session_factory, temp_storage_dir: Path) -> TestClient:
    """Provide a FastAPI TestClient with overridden get_db and get_settings dependencies."""

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_get_settings():
        return Settings(storage_dir=str(temp_storage_dir))

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = override_get_settings
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()



def test_create_job_success(client: TestClient, session_factory) -> None:
    """Verify successful job creation returns 201 Created and persists records."""
    payload = {
        "event_name": "Cloud Native Architecture 2026",
        "recipients": [
            {"name": "Alice Johnson", "email": "alice@example.com"},
            {"name": "Bob Smith", "email": "bob@example.com"},
        ],
    }

    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()

    assert "id" in data
    job_id = uuid.UUID(data["id"])
    assert data["event_name"] == "Cloud Native Architecture 2026"
    assert data["status"] == "PENDING"
    assert data["total_count"] == 2
    assert "created_at" in data

    # Verify directly in the database that automatic processing completed the job
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.total_count == 2
        assert job.processed_count == 2
        assert job.successful_count == 2
        assert job.failed_count == 0
        assert len(job.recipients) == 2
        emails = {r.recipient_email for r in job.recipients}
        assert emails == {"alice@example.com", "bob@example.com"}
        for r in job.recipients:
            assert r.status == RecipientStatus.COMPLETED
            assert r.certificate_code is not None
            assert r.pdf_path is not None


def test_create_job_empty_recipients_allowed(client: TestClient, session_factory) -> None:
    """Verify job creation with zero recipients is accepted and creates total_count=0."""
    payload = {
        "event_name": "Zero Recipient Event",
        "recipients": [],
    }

    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()

    assert data["total_count"] == 0
    assert data["status"] == "PENDING"

    job_id = uuid.UUID(data["id"])
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.total_count == 0
        assert len(job.recipients) == 0


def test_create_job_validation_missing_event_name(client: TestClient) -> None:
    """Verify validation error when event_name is missing or empty."""
    response = client.post("/jobs", json={"event_name": "", "recipients": []})
    assert response.status_code == 422


def test_create_job_validation_malformed_recipient(client: TestClient) -> None:
    """Verify validation error when recipient has invalid email or empty name."""
    # Invalid email
    response_bad_email = client.post(
        "/jobs",
        json={
            "event_name": "Event",
            "recipients": [{"name": "User", "email": "not-an-email"}],
        },
    )
    assert response_bad_email.status_code == 422

    # Empty name
    response_bad_name = client.post(
        "/jobs",
        json={
            "event_name": "Event",
            "recipients": [{"name": "   ", "email": "valid@example.com"}],
        },
    )
    assert response_bad_name.status_code == 422


def test_create_job_exceeds_max_recipients(client: TestClient) -> None:
    """Verify that batches exceeding MAX_RECIPIENTS_PER_JOB (500) are rejected with 422."""
    too_many_recipients = [
        {"name": f"Recipient {i}", "email": f"user{i}@example.com"}
        for i in range(MAX_RECIPIENTS_PER_JOB + 1)
    ]
    response = client.post(
        "/jobs",
        json={"event_name": "Large Event", "recipients": too_many_recipients},
    )
    assert response.status_code == 422


def test_create_job_database_rollback_on_failure(client: TestClient, session_factory) -> None:
    """Verify that if persisting recipients fails, entire transaction rolls back."""
    # Force failure by inserting a duplicate recipient where certificate_code or DB constraint fails
    # Alternatively, simulate a flush error by overriding Session.add_all
    with session_factory() as session:
        initial_job_count = session.query(GenerationJob).count()

    # Pass an invalid payload at DB level by mocking or patching
    class ErrorSession(Session):
        def add_all(self, instances):
            raise RuntimeError("Database disk failure during recipient insertion")

    failing_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=session_factory.kw["bind"], class_=ErrorSession
    )

    def override_failing_db():
        db = failing_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_failing_db
    try:
        with pytest.raises(RuntimeError):
            client.post(
                "/jobs",
                json={
                    "event_name": "Rollback Event",
                    "recipients": [{"name": "User", "email": "user@test.com"}],
                },
            )
    finally:
        app.dependency_overrides.clear()

    # Verify no partial job was created
    with session_factory() as session:
        current_job_count = session.query(GenerationJob).count()
        assert current_job_count == initial_job_count


def test_get_job_status_success(client: TestClient, session_factory) -> None:
    """Verify GET /jobs/{job_id} returns accurate counter and progress metrics."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Progress Tracking Event",
            total_count=4,
            processed_count=2,
            successful_count=2,
            failed_count=0,
            status=JobStatus.PROCESSING,
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()

    assert data["id"] == str(job_id)
    assert data["event_name"] == "Progress Tracking Event"
    assert data["status"] == "PROCESSING"
    assert data["total_count"] == 4
    assert data["processed_count"] == 2
    assert data["successful_count"] == 2
    assert data["failed_count"] == 0
    assert data["progress_percentage"] == 50.0
    assert data["started_at"] is not None
    assert data["completed_at"] is None


def test_get_job_status_zero_recipients_safe(client: TestClient, session_factory) -> None:
    """Verify progress calculation does not raise ZeroDivisionError for total_count=0."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Zero Recipient Completed Event",
            total_count=0,
            processed_count=0,
            successful_count=0,
            failed_count=0,
            status=JobStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()

    assert data["total_count"] == 0
    assert data["progress_percentage"] == 100.0


def test_get_job_status_not_found(client: TestClient) -> None:
    """Verify GET /jobs/{job_id} returns 404 for non-existent job UUID."""
    random_id = uuid.uuid4()
    response = client.get(f"/jobs/{random_id}")
    assert response.status_code == 404
    assert f"Job with ID '{random_id}' not found." in response.json()["detail"]


def test_get_job_status_invalid_uuid(client: TestClient) -> None:
    """Verify GET /jobs/{job_id} returns 422 for malformed UUID in path."""
    response = client.get("/jobs/not-a-valid-uuid")
    assert response.status_code == 422


def test_get_certificate_success(client: TestClient, session_factory, temp_storage_dir: Path) -> None:
    """Verify GET /jobs/{job_id}/recipients/{recipient_id}/certificate returns 200 with PDF payload."""
    pdf_filename = "cert_alice.pdf"
    pdf_file = temp_storage_dir / pdf_filename
    pdf_content = b"%PDF-1.4 mock binary certificate content"
    pdf_file.write_bytes(pdf_content)

    with session_factory() as session:
        job = GenerationJob(event_name="Graduation Ceremony", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Alice Johnson",
            recipient_email="alice@example.com",
            status=RecipientStatus.COMPLETED,
            certificate_code="CERT-ALICE-123",
            pdf_path=pdf_filename,
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.get(f"/jobs/{job_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment; filename=\"CERT-ALICE-123.pdf\"" in response.headers.get("content-disposition", "")
    assert response.content == pdf_content


def test_get_certificate_unknown_job(client: TestClient, session_factory) -> None:
    """Verify 404 when requested job_id does not exist."""
    random_job_id = uuid.uuid4()
    random_recipient_id = uuid.uuid4()
    response = client.get(f"/jobs/{random_job_id}/recipients/{random_recipient_id}/certificate")
    assert response.status_code == 404
    assert f"Job with ID '{random_job_id}' not found." in response.json()["detail"]


def test_get_certificate_unknown_recipient(client: TestClient, session_factory) -> None:
    """Verify 404 when recipient_id does not exist in the requested job."""
    with session_factory() as session:
        job = GenerationJob(event_name="Summit", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

    random_recipient_id = uuid.uuid4()
    response = client.get(f"/jobs/{job_id}/recipients/{random_recipient_id}/certificate")
    assert response.status_code == 404
    assert f"Recipient with ID '{random_recipient_id}' not found for job '{job_id}'." in response.json()["detail"]


def test_get_certificate_recipient_belongs_to_different_job(client: TestClient, session_factory) -> None:
    """Verify 404 when recipient exists but belongs to a different job."""
    with session_factory() as session:
        job_1 = GenerationJob(event_name="Job 1", total_count=1)
        job_2 = GenerationJob(event_name="Job 2", total_count=1)
        session.add_all([job_1, job_2])
        session.commit()

        recipient = RecipientCertificate(
            job_id=job_1.id,
            recipient_name="User",
            recipient_email="user@test.com",
            status=RecipientStatus.COMPLETED,
            pdf_path="test.pdf",
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id
        job_2_id = job_2.id

    # Request recipient under job_2
    response = client.get(f"/jobs/{job_2_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 404
    assert f"Recipient with ID '{recipient_id}' not found for job '{job_2_id}'." in response.json()["detail"]


@pytest.mark.parametrize("status_val", [RecipientStatus.PENDING, RecipientStatus.PROCESSING, RecipientStatus.FAILED])
def test_get_certificate_not_completed_returns_400(client: TestClient, session_factory, status_val) -> None:
    """Verify 400 Bad Request when certificate generation is not in COMPLETED status."""
    with session_factory() as session:
        job = GenerationJob(event_name="Ongoing Workshop", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Pending User",
            recipient_email="pending@test.com",
            status=status_val,
            error_message="Font rendering failed" if status_val == RecipientStatus.FAILED else None,
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.get(f"/jobs/{job_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert f"Certificate for recipient '{recipient_id}' is not available." in detail
    assert status_val.value in detail


def test_get_certificate_file_missing_on_disk_returns_404(client: TestClient, session_factory) -> None:
    """Verify 404 when record is COMPLETED but physical PDF file is missing from storage."""
    with session_factory() as session:
        job = GenerationJob(event_name="Ghost File Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Ghost User",
            recipient_email="ghost@test.com",
            status=RecipientStatus.COMPLETED,
            certificate_code="CERT-GHOST-1",
            pdf_path="non_existent_file.pdf",
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.get(f"/jobs/{job_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 404
    assert "Certificate file not found on storage." in response.json()["detail"]


def test_get_certificate_path_traversal_returns_404(
    client: TestClient, session_factory, tmp_path: Path
) -> None:
    """Verify 404 when recipient pdf_path attempts directory traversal outside storage."""
    # Create a secret file outside the configured storage directory
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("classified data")

    with session_factory() as session:
        job = GenerationJob(event_name="Traversal Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Hacker",
            recipient_email="hack@test.com",
            status=RecipientStatus.COMPLETED,
            certificate_code="CERT-HACK-1",
            pdf_path="../../secret.txt",
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.get(f"/jobs/{job_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 404
    assert "Certificate file path is invalid." in response.json()["detail"]


def test_create_job_with_custom_issue_date_persisted_and_rendered(
    client: TestClient, session_factory, temp_storage_dir: Path
) -> None:
    """Verify that a supplied issue_date is persisted and automatically processed onto rendered certificates."""
    payload = {
        "event_name": "International Tech Summit 2025",
        "issue_date": "2025-06-15",
        "recipients": [
            {"name": "Alice Developer", "email": "alice@example.com"}
        ],
    }
    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()
    job_id = uuid.UUID(data["id"])
    assert data["issue_date"] == "2025-06-15"
    assert data["status"] == "PENDING"

    # Verify status API reflects completed automatic processing and persisted issue_date
    status_response = client.get(f"/jobs/{job_id}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "COMPLETED"
    assert status_data["issue_date"] == "2025-06-15"
    assert status_data["total_count"] == 1
    assert status_data["processed_count"] == 1
    assert status_data["successful_count"] == 1

    # Verify duplicate processing is rejected by existing claim/status mechanism
    processor = JobProcessorService(
        session_factory=session_factory,
        generator_service=CertificateGeneratorService(storage_dir=temp_storage_dir),
    )
    with pytest.raises(JobAlreadyCompletedError):
        processor.process_job(job_id)

    # Inspect recipient record and PDF
    with session_factory() as session:
        recipient = session.query(RecipientCertificate).filter_by(job_id=job_id).one()
        assert recipient.status == RecipientStatus.COMPLETED
        pdf_path = temp_storage_dir / recipient.pdf_path
        assert pdf_path.exists()

    # Extract PDF text and verify the formatted date appears
    reader = pypdf.PdfReader(pdf_path)
    extracted_text = reader.pages[0].extract_text()
    assert "Alice Developer" in extracted_text
    assert "International Tech Summit 2025" in extracted_text
    assert "June 15, 2025" in extracted_text


def test_create_job_without_issue_date_uses_default_date(
    client: TestClient, session_factory, temp_storage_dir: Path
) -> None:
    """Verify that omitting issue_date preserves default behavior using job creation date."""
    payload = {
        "event_name": "Autumn Coding Workshop",
        "recipients": [
            {"name": "Bob Builder", "email": "bob@example.com"}
        ],
    }
    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()
    job_id = uuid.UUID(data["id"])
    assert data["issue_date"] is None
    assert data["status"] == "PENDING"

    # Status API returns COMPLETED for automatically processed job
    status_response = client.get(f"/jobs/{job_id}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "COMPLETED"
    assert status_data["issue_date"] is None
    assert status_data["successful_count"] == 1

    # Verify duplicate processing is rejected by existing claim/status mechanism
    processor = JobProcessorService(
        session_factory=session_factory,
        generator_service=CertificateGeneratorService(storage_dir=temp_storage_dir),
    )
    with pytest.raises(JobAlreadyCompletedError):
        processor.process_job(job_id)

    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        recipient = session.query(RecipientCertificate).filter_by(job_id=job_id).one()
        assert recipient.status == RecipientStatus.COMPLETED
        pdf_path = temp_storage_dir / recipient.pdf_path
        assert pdf_path.exists()
        expected_date_str = job.created_at.strftime("%B %d, %Y")

    reader = pypdf.PdfReader(pdf_path)
    extracted_text = reader.pages[0].extract_text()
    assert "Bob Builder" in extracted_text
    assert expected_date_str in extracted_text


def test_get_certificate_error_sanitization_does_not_leak_internal_details(
    client: TestClient, session_factory
) -> None:
    """Verify certificate download does not expose sensitive internal error messages or stack traces."""
    sensitive_internal_error = (
        "OperationalError: password authentication failed for user 'postgres' "
        "(pwd=secret_prod_pw_9988) at /var/app/internal/db_connection.py:42"
    )

    with session_factory() as session:
        job = GenerationJob(event_name="Security Audit Event", total_count=1)
        session.add(job)
        session.commit()
        job_id = job.id

        recipient = RecipientCertificate(
            job_id=job_id,
            recipient_name="Eve Auditor",
            recipient_email="eve@audit.org",
            status=RecipientStatus.FAILED,
            error_message=sensitive_internal_error,
        )
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.get(f"/jobs/{job_id}/recipients/{recipient_id}/certificate")
    assert response.status_code == 400

    # Verify sensitive internals are sanitized from the HTTP response
    assert "secret_prod_pw_9988" not in response.text
    assert "db_connection.py" not in response.text
    assert "OperationalError" not in response.text

    # Safe error message returned to user
    detail = response.json()["detail"]
    assert f"Certificate for recipient '{recipient_id}' is not available." in detail
    assert "Status: 'FAILED'" in detail

    # Confirm the raw error message is still preserved in the persisted database record
    with session_factory() as session:
        saved_recipient = session.get(RecipientCertificate, recipient_id)
        assert saved_recipient.error_message == sensitive_internal_error


def test_create_job_automatic_processing_partial_failure(
    client: TestClient, session_factory, temp_storage_dir: Path
) -> None:
    """Verify automatic background processing handles per-recipient failures without stopping other recipients."""
    from unittest.mock import patch
    from app.services.certificate_generator import CertificateGenerationError

    real_generate = CertificateGeneratorService.generate_certificate

    def mock_generate(self, recipient_name, event_name, issue_date, custom_code=None):
        if recipient_name == "Failing Participant":
            raise CertificateGenerationError("Simulated ReportLab rendering failure")
        return real_generate(self, recipient_name, event_name, issue_date, custom_code)

    payload = {
        "event_name": "Resilient Processing Workshop",
        "recipients": [
            {"name": "Failing Participant", "email": "fail@example.com"},
            {"name": "Winning Participant", "email": "win@example.com"},
        ],
    }

    with patch.object(CertificateGeneratorService, "generate_certificate", mock_generate):
        response = client.post("/jobs", json=payload)

    assert response.status_code == 201
    data = response.json()
    job_id = uuid.UUID(data["id"])
    assert data["status"] == "PENDING"
    assert data["total_count"] == 2

    # Status endpoint reports PARTIALLY_COMPLETED
    status_response = client.get(f"/jobs/{job_id}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "PARTIALLY_COMPLETED"
    assert status_data["total_count"] == 2
    assert status_data["processed_count"] == 2
    assert status_data["successful_count"] == 1
    assert status_data["failed_count"] == 1
    assert status_data["progress_percentage"] == 100.0

    # Verify recipient outcomes directly in database
    with session_factory() as session:
        recipients = {
            r.recipient_name: r
            for r in session.query(RecipientCertificate).filter_by(job_id=job_id).all()
        }
        failed_rec = recipients["Failing Participant"]
        success_rec = recipients["Winning Participant"]

        assert failed_rec.status == RecipientStatus.FAILED
        assert "Simulated ReportLab rendering failure" in failed_rec.error_message
        assert failed_rec.pdf_path is None

        assert success_rec.status == RecipientStatus.COMPLETED
        assert success_rec.pdf_path is not None
        assert (temp_storage_dir / success_rec.pdf_path).exists()
